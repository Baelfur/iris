"""Dynamic-query handler — orchestrates the request lifecycle in fixed phases.

Auth → schema-allowlist → required-param check → DDL validation →
SQL compile → execute → response shape. Each phase is its own helper
so the orchestration reads as a sequence rather than a 160-line block,
and so future hygiene PRs that touch a single phase don't have to
re-grok the whole function.
"""

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth.creds import extract_basic_creds
from ..auth.user import verify_token
from ..context import AppContext, get_context
from ..engine import cursor as cursor_mod
from ..engine import expression, schema_cache
from ..engine.circuit_breaker import CircuitBreakerOpen, fetch_with_breaker
from ..engine.cursor import CursorError
from ..engine.expression import ExpressionError
from ..engine.query_engine import QueryParams, build_links, build_query, extract_simple_filters
from ..errors.exceptions import DatabaseError
from ..errors.messages import (
    TITLE_BAD_REQUEST,
    TITLE_NOT_FOUND,
    TITLE_UNAUTHORIZED,
    db_error_body,
    error_msg,
)
from ..loaders import validation as view_defs
from ..redact import redact_username

if TYPE_CHECKING:
    from ..config.settings import AppSettings

logger = logging.getLogger(__name__)


# --- phase helpers ---------------------------------------------------------


def _check_having_requires_groupby(having: str | None, groupby: str | None) -> None:
    """Reject 400 when ``$having`` is supplied without ``$groupby``.

    ``$having`` filters the aggregated groups produced by ``$groupby``;
    without an aggregation, there's no group-level predicate to apply.
    The check happens early so the caller's mistake surfaces before any
    DDL or column validation runs.
    """
    if having and not groupby:
        raise HTTPException(400, error_msg("$having requires $groupby", TITLE_BAD_REQUEST))


def _enforce_view_def(
    schema: str,
    view_name: str,
    simple_filters: dict,
    filter_: str | None,
) -> None:
    """If a view def exists for ``schema.view_name``, enforce its required +
    optional param contracts.

    Required-param check considers BOTH simple filters AND columns
    equality-constrained by ``$filter``. ``$having`` is intentionally
    not counted — it constrains aggregated groups, not rows, so it doesn't
    honor the YAML's "client must filter rows by this" intent.
    """
    vdef = view_defs.get_def(schema, view_name)
    if not vdef:
        return

    def _ident_validator(col: str) -> str | None:
        # Expression parser wants a flat string error; did_you_mean
        # hints aren't surfaced inside ExpressionError text.
        err = schema_cache.validate_columns(schema, view_name, [col])
        return err["message"] if err else None

    filter_constrained: set = set()
    if filter_:
        try:
            filter_constrained = expression.constrained_columns_in(
                filter_,
                _ident_validator,
            )
        except ExpressionError as exc:
            raise HTTPException(400, error_msg(str(exc), TITLE_BAD_REQUEST)) from exc

    err = vdef.satisfied_by(set(simple_filters.keys()), filter_constrained)
    if err:
        raise HTTPException(400, error_msg(err, TITLE_BAD_REQUEST))

    if simple_filters:
        err = vdef.validate(simple_filters)
        if err:
            raise HTTPException(400, error_msg(err, TITLE_BAD_REQUEST))


def _check_table_exists(schema: str, view_name: str) -> None:
    """Look up ``schema.view_name`` in the DDL cache and 404 if absent.

    Gate for the column-level validators that follow — every helper
    after this assumes the table exists. ``_check_schema_allowed`` runs
    earlier and gates on the env-var allowlist; this one catches tables
    in an allowed schema that don't exist or that the startup DDL
    harvest didn't include (revoked grants, recently dropped tables).
    """
    err = schema_cache.validate_table(schema, view_name)
    if err:
        detail = {**err, "message": error_msg(err["message"], TITLE_NOT_FOUND)}
        raise HTTPException(404, detail=detail)


def _normalize_select(schema: str, view_name: str, select: str | None) -> str | None:
    """Validate every column in ``$select`` and return a normalized form.

    Returns the canonical comma-joined string for ``build_query`` to emit.
    ``None`` (no ``$select`` supplied) and the literal ``"*"`` pass through
    unchanged — both mean "every column" and ``build_query`` handles the
    SELECT * shape downstream. The normalization strips per-column
    whitespace so trailing/leading spaces in the URL grammar don't survive
    into the SQL output.
    """
    if not select or select == "*":
        return select
    cols = schema_cache.parse_column_list(select)
    err = schema_cache.validate_columns(schema, view_name, cols)
    if err:
        detail = {**err, "message": error_msg(err["message"], TITLE_BAD_REQUEST)}
        raise HTTPException(400, detail=detail)
    return ", ".join(cols)


def _normalize_orderby(schema: str, view_name: str, orderby: str | None) -> str | None:
    """Parse ``$orderby`` into ``(column, direction)`` pairs and emit
    normalized SQL.

    Direction is uppercased ``ASC`` / ``DESC`` or empty (default DB
    ordering). ``parse_orderby_list`` raises ``ValueError`` if a token
    is something other than a valid direction; that surfaces as a 400
    here. ``None`` passes through unchanged (no ORDER BY clause).
    """
    if not orderby:
        return orderby
    try:
        orderby_items = schema_cache.parse_orderby_list(orderby)
    except ValueError as exc:
        raise HTTPException(400, error_msg(str(exc), TITLE_BAD_REQUEST)) from exc
    cols = [col for col, _ in orderby_items]
    err = schema_cache.validate_columns(schema, view_name, cols)
    if err:
        detail = {**err, "message": error_msg(err["message"], TITLE_BAD_REQUEST)}
        raise HTTPException(400, detail=detail)
    return ", ".join(f"{col} {d}".strip() for col, d in orderby_items)


def _validate_groupby(
    schema: str,
    view_name: str,
    select: str | None,
    groupby: str | None,
) -> None:
    """``$groupby`` requires explicit ``$select`` and that every selected
    column also appears in the group-by list. No aggregate functions in
    the grammar — ``$groupby`` produces DISTINCT-style "what values exist"
    queries; ``COUNT/SUM`` are operator-authored custom queries."""
    if not groupby:
        return
    groupby_cols = schema_cache.parse_column_list(groupby)
    err = schema_cache.validate_columns(schema, view_name, groupby_cols)
    if err:
        detail = {**err, "message": error_msg(err["message"], TITLE_BAD_REQUEST)}
        raise HTTPException(400, detail=detail)
    if not select or select == "*":
        raise HTTPException(
            400, error_msg("$groupby requires an explicit $select", TITLE_BAD_REQUEST)
        )
    select_cols = schema_cache.parse_column_list(select)
    groupby_set = {c.lower() for c in groupby_cols}
    extra = [c for c in select_cols if c.lower() not in groupby_set]
    if extra:
        raise HTTPException(
            400,
            error_msg(
                f"$select contains column(s) not in $groupby: {', '.join(extra)}", TITLE_BAD_REQUEST
            ),
        )


def _validate_simple_filter_columns(
    schema: str,
    view_name: str,
    simple_filters: dict,
) -> None:
    """Every ``?col=val`` simple-filter column must exist on the table.

    Runs after the view-def required-param check so a missing
    required param surfaces as ``Required parameter(s) missing: …`` —
    the more actionable error — rather than ``Invalid column(s): …``
    when the YAML required a column the caller didn't filter on.
    """
    if not simple_filters:
        return
    err = schema_cache.validate_columns(schema, view_name, list(simple_filters.keys()))
    if err:
        detail = {**err, "message": error_msg(err["message"], TITLE_BAD_REQUEST)}
        raise HTTPException(400, detail=detail)


def _apply_max_page_cap(settings: "AppSettings", count: int | None) -> int | None:
    """Clamp ``$count`` to ``MAX_PAGE_SIZE`` when the operator has set a cap.

    Returns the original count, the cap (when count was unset or above
    the cap), or the original count when no cap is configured. The cap
    is silent — clients receive a smaller-than-requested page along
    with a ``next`` link to walk the rest, not a 4xx. ``MAX_PAGE_SIZE=0``
    (the default) disables the cap entirely.
    """
    if settings.max_page_size > 0 and (count is None or count > settings.max_page_size):
        return settings.max_page_size
    return count


def _compile_sql(
    schema: str,
    view_name: str,
    params: QueryParams,
    ctx: AppContext,
    catalog: str | None,
) -> tuple[str, object]:
    """Build the parameterized SQL + binds for the configured paramstyle,
    surfacing parser errors as 400s.

    ``build_query`` is paramstyle-aware: ``pyformat`` dict for postgres /
    mysql / mariadb, ``named`` dict for oracle, ``qmark`` positional list
    for trino. The ``catalog`` argument is only set by the Trino 3-segment
    route (``/{catalog}/{schema}/{view_name}``); other variants pass None
    and emit 2-segment qualifiers. ``ExpressionError`` from the closed-
    grammar ``$filter`` / ``$having`` parser becomes a 400 here so the
    caller can rely on a uniform return shape.
    """
    try:
        return build_query(
            schema,
            view_name,
            params,
            paramstyle=ctx.paramstyle,
            catalog=catalog,
        )
    except ExpressionError as exc:
        raise HTTPException(400, error_msg(str(exc), TITLE_BAD_REQUEST)) from exc


def _format_db_error_response(
    schema: str,
    view_name: str,
    exc: DatabaseError,
    ctx: AppContext,
    creds: tuple[str, str] | None,
) -> JSONResponse:
    """Redact usernames out of the driver text, log the full version, and
    return the public-facing JSON body shaped per ``ERROR_DETAIL``.

    Service-account username is always redacted (driver text often echoes
    it on permission errors); passthrough ``creds[0]`` is additionally
    redacted when present.
    """
    detail = str(exc)
    if ctx.db_user:
        detail = redact_username(detail, ctx.db_user, ctx.settings.log_user_secret)
    if creds:
        detail = redact_username(detail, creds[0], ctx.settings.log_user_secret)
    logger.error("Database error on %s.%s: %s", schema, view_name, detail)
    return JSONResponse(status_code=400, content=db_error_body(detail))


def _shape_response(
    view_name: str,
    params: QueryParams,
    rows: list,
    next_cursor: str | None = None,
) -> dict:
    """Build the success-path response envelope: ``name`` + ``elements``,
    plus ``links`` when more pages exist, plus ``cursor`` when keyset
    pagination produced a next-page marker.

    The ``links`` and ``cursor`` keys are omitted entirely (not set to
    empty / None) when there's no next page so consumers can branch on
    dict-key presence. ``build_links`` decides whether the page is full
    and emits the next-page URL — with ``$cursor`` when ``next_cursor``
    is supplied, falling back to ``$start_index`` advancement otherwise.
    """
    result: dict = {"name": view_name, "elements": rows}
    links = build_links(params, len(rows), cursor=next_cursor)
    if links:
        result["links"] = links
    if next_cursor is not None:
        result["cursor"] = next_cursor
    return result


def _parse_orderby_for_cursor(orderby_normalized: str) -> list[tuple[str, str]]:
    """Re-parse the normalized $orderby into ``(col, dir)`` pairs.

    The handler has already normalized $orderby via
    :func:`_normalize_orderby` (which itself uses
    ``schema_cache.parse_orderby_list``). Re-parsing here keeps the
    cursor module decoupled from ``schema_cache`` — the parser sees the
    canonical comma-joined form and returns the pair list the keyset
    SQL builder needs.
    """
    items: list[tuple[str, str]] = []
    for raw in orderby_normalized.split(","):
        parts = raw.strip().split()
        col = parts[0]
        direction = parts[1].upper() if len(parts) > 1 else "ASC"
        items.append((col, direction))
    return items


def _resolve_cursor(
    cursor_token: str | None,
    orderby_normalized: str | None,
    start_index: int | None,
    settings: "AppSettings",
) -> tuple[list[tuple[str, str]], list] | None:
    """Validate the request's cursor against $orderby + $start_index and
    return the keyset (col-direction pairs, values) tuple, or None when
    no cursor is in play.

    Surface errors as HTTP 400 — bad format, signature failure, orderby
    mismatch, mutually-exclusive with $start_index, or missing
    $orderby all funnel to the same client-facing failure mode.
    """
    if cursor_token is None:
        return None
    if start_index is not None:
        raise HTTPException(
            400,
            error_msg(
                "$cursor and $start_index are mutually exclusive; use one or the other",
                TITLE_BAD_REQUEST,
            ),
        )
    if not orderby_normalized:
        raise HTTPException(
            400,
            error_msg(
                "$cursor requires $orderby (cursors are bound to the orderby clause)",
                TITLE_BAD_REQUEST,
            ),
        )
    secret = cursor_mod.get_secret(settings.cursor_secret)
    try:
        values = cursor_mod.parse_cursor(cursor_token, orderby_normalized, secret)
    except CursorError as exc:
        raise HTTPException(400, error_msg(str(exc), TITLE_BAD_REQUEST)) from exc
    cols_dirs = _parse_orderby_for_cursor(orderby_normalized)
    if len(values) != len(cols_dirs):
        raise HTTPException(
            400,
            error_msg(
                "$cursor value count does not match $orderby column count",
                TITLE_BAD_REQUEST,
            ),
        )
    return cols_dirs, values


def _next_cursor(
    rows: list,
    params: QueryParams,
    settings: "AppSettings",
) -> str | None:
    """Mint a cursor for the next page when this one is full and we have
    an $orderby that lets a keyset walk continue.

    Returns None when:
      - no $orderby (we can't make a stable cursor)
      - page isn't full (no next page)
      - $count wasn't set (no concept of full-page)
      - last row is missing any orderby column (shouldn't happen given
        validation, but fail safe rather than emit a broken token)
    """
    if not params.orderby or params.count is None or len(rows) < params.count:
        return None
    last = rows[-1]
    cols_dirs = _parse_orderby_for_cursor(params.orderby)
    try:
        values = [last[col] for col, _ in cols_dirs]
    except (KeyError, TypeError):
        return None
    secret = cursor_mod.get_secret(settings.cursor_secret)
    return cursor_mod.make_cursor(params.orderby, values, secret)


# --- handler ---------------------------------------------------------------


async def query_view_impl(
    request: Request,
    schema: str,
    view_name: str,
    select: str | None,
    filter_: str | None,
    orderby: str | None,
    count: int | None,
    start_index: int | None,
    groupby: str | None,
    having: str | None,
    catalog: str | None = None,
    cursor: str | None = None,
):
    """Core dynamic-query handler. Routes register thin wrappers that delegate here.

    The default 2-segment route ``/{schema}/{view_name}`` (registered in
    :mod:`core.routes.inventory`) calls this with ``catalog=None``. The
    Trino variant registers an additional 3-segment route
    ``/{catalog}/{schema}/{view_name}`` that passes the catalog through to
    ``build_query`` for fully-qualified ``catalog.schema.table`` SQL emission.
    Variant-side validation (e.g., catalog matches the connection's configured
    catalog) is the caller's responsibility — the DDL cache is keyed on the
    configured catalog only, so unknown-catalog requests must be rejected before
    schema/table validation runs.
    """
    await verify_token(request)
    ctx = get_context()
    settings = ctx.settings

    _check_having_requires_groupby(having, groupby)

    simple_filters = extract_simple_filters(dict(request.query_params))
    _enforce_view_def(schema, view_name, simple_filters, filter_)
    _check_table_exists(schema, view_name)

    select = _normalize_select(schema, view_name, select)
    orderby = _normalize_orderby(schema, view_name, orderby)
    _validate_groupby(schema, view_name, select, groupby)
    _validate_simple_filter_columns(schema, view_name, simple_filters)

    count = _apply_max_page_cap(settings, count)

    cursor_keyset = _resolve_cursor(cursor, orderby, start_index, settings)

    params = QueryParams(
        select=select,
        filter_=filter_,
        orderby=orderby,
        count=count,
        start_index=start_index,
        simple_filters=simple_filters,
        groupby=groupby,
        having=having,
        cursor_keyset=cursor_keyset,
    )
    sql, binds = _compile_sql(schema, view_name, params, ctx, catalog)

    creds = extract_basic_creds(request)
    if creds is None and settings.auth.require_passthrough:
        raise HTTPException(
            401,
            error_msg(
                "Passthrough credentials required (X-DB-Authorization or "
                "Authorization: Basic). Set AUTH__REQUIRE_PASSTHROUGH=false "
                "to permit pool-mode access.",
                TITLE_UNAUTHORIZED,
            ),
        )
    try:
        rows = await fetch_with_breaker(ctx, sql, binds, creds)
    except CircuitBreakerOpen as exc:
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(exc.retry_after)},
            content={"error": "service temporarily unavailable"},
        )
    except DatabaseError as exc:
        return _format_db_error_response(schema, view_name, exc, ctx, creds)

    next_cur = _next_cursor(rows, params, settings)
    return _shape_response(view_name, params, rows, next_cursor=next_cur)
