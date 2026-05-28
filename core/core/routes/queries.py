"""Custom query endpoints — SQL defined in YAML files.

Two routers:

- ``router`` exposes ``GET /queries/{path:path}`` on the dev-facing
  surface — devs invoke individual operator-curated queries.
- ``admin_router`` exposes ``GET /queries`` (the list endpoint) on the
  admin sub-app — listing the catalog is operator-facing recon and was
  always admin-token-gated; mounting it under ``/admin/queries``
  matches the auth posture and keeps it out of the dev OpenAPI spec.

The execution endpoint is hidden from the dev OpenAPI spec via
``include_in_schema=False``; the per-deployment dynamic spec
(``openapi_dynamic.py``) injects one concrete operation entry per
registered query so devs see real URLs in ``/docs``.
"""

import difflib
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth.admin import verify_admin_access
from ..auth.creds import extract_basic_creds
from ..auth.user import verify_token
from ..context import get_context
from ..engine.circuit_breaker import CircuitBreakerOpen, fetch_with_breaker
from ..engine.paramstyle import BindAccumulator
from ..engine.query_engine import extract_simple_filters
from ..errors.exceptions import DatabaseError
from ..errors.messages import (
    TITLE_BAD_REQUEST,
    TITLE_NOT_FOUND,
    TITLE_UNAUTHORIZED,
    db_error_body,
    error_msg,
)
from ..loaders.queries import get_query, list_queries
from ..redact import redact_username

router = APIRouter(prefix="/queries", tags=["queries"])
admin_router = APIRouter(prefix="/queries", tags=["queries"])
logger = logging.getLogger(__name__)


@admin_router.get("")
async def list_available_queries(request: Request):
    """List all registered custom queries.

    Mounted under the admin sub-app at ``/admin/queries``. End users
    are given the named query URLs they're meant to call; they don't
    enumerate. Listing the catalog over an unauthenticated channel
    discloses operator-authored query names that are useful pre-attack
    reconnaissance.
    """
    await verify_admin_access(request)
    return {"queries": [f"/queries/{q}" for q in list_queries()]}


@router.get("/{path:path}", include_in_schema=False)
async def run_query(request: Request, path: str):
    """Execute a custom SQL query defined in a YAML file."""
    await verify_token(request)
    ctx = get_context()

    qdef = get_query(path)
    if not qdef:
        not_found: dict = {"message": error_msg(f"Query '{path}' not found", TITLE_NOT_FOUND)}
        matches = difflib.get_close_matches(path, list_queries(), n=1, cutoff=0.6)
        if matches:
            not_found["did_you_mean"] = matches[0]
        raise HTTPException(404, detail=not_found)

    params = extract_simple_filters(dict(request.query_params))

    # Custom queries don't accept $filter, so satisfied_by only considers
    # the simple-filter keys. validate handles the unknown-param check
    # now split.
    err = qdef.view_def.satisfied_by(set(params.keys()), set())
    if err:
        raise HTTPException(400, error_msg(err, TITLE_BAD_REQUEST))
    err = qdef.view_def.validate(params)
    if err:
        raise HTTPException(400, error_msg(err, TITLE_BAD_REQUEST))

    sql, binds = _substitute_params(qdef.sql, params, ctx.paramstyle)

    creds = extract_basic_creds(request)
    if creds is None and ctx.settings.auth.require_passthrough:
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
        detail = str(exc)
        # Redact both potential username sources before logging. See
        # the matching block in routes/inventory.py for the rationale.
        #
        if ctx.db_user:
            detail = redact_username(detail, ctx.db_user, ctx.settings.log_user_secret)
        if creds:
            detail = redact_username(detail, creds[0], ctx.settings.log_user_secret)
        logger.error("Query error on %s: %s", path, detail)
        return JSONResponse(status_code=400, content=db_error_body(detail))

    return {"name": qdef.name, "elements": rows}


def _substitute_params(sql: str, params: dict, paramstyle: str):
    """Translate YAML ``:name`` placeholders to the driver's paramstyle.

        Returns (sql, binds). Binds is a dict keyed by the operator's own param
        name for pyformat/named, a list for qmark (ordered by appearance in SQL).

        Bind keys match the operator's param name verbatim — no synthesized
        prefix. This means a literal token in the YAML SQL (e.g. ``:p_level``
        inside a string literal) cannot collide with a synthesized placeholder
    . Walks the SQL once via :class:`BindAccumulator` so the three
        paramstyle branches are unified with the simple-filter and
        expression-parser bind paths.
    """
    acc = BindAccumulator(paramstyle)

    def replace(match):
        name = match.group(1)
        if name in params:
            return acc.bind(params[name], key=name)
        return match.group(0)

    sql = re.sub(r":(\w+)", replace, sql)
    return sql, acc.binds
