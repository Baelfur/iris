"""Per-deployment dynamic OpenAPI spec generation.

The dev-facing ``/openapi.json`` is synthesized from the harvested DDL
cache so consumers see concrete routes per table — ``/public/products``,
``/public/orders`` — rather than the generic ``/{schema}/{view_name}``
placeholder. Each registered custom query likewise becomes a concrete
operation entry. Wired in ``app_meta.build_app``; invalidated on
``/admin/refresh-schema`` and ``/admin/reload-config``.

Also injects the per-deployment security schemes so Swagger UI's
"Authorize" dialog is wired up:

- Dev side: ``Authorization: Bearer <jwt>`` (only when ``AUTH__JWKS_URL``
  is configured) and ``X-DB-Authorization: Basic <b64>`` (always
  optional, for per-caller DB-credential passthrough).
- Admin side: ``X-Admin-Token`` (when ``AUTH__ADMIN_TOKEN`` is set;
  endpoints fail closed when it isn't).
"""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from . import aliases as alias_routes
from .catalog import iter_queries, iter_tables
from .engine.query_params import openapi_dicts as closed_grammar_openapi_dicts

# Security scheme keys reused across spec generation. Names appear in
# the "Authorize" dropdown — short, descriptive forms.
_JWT_SCHEME = "JWT"
_PASSTHROUGH_BASIC_SCHEME = "PassthroughBasic"
_PASSTHROUGH_XDB_SCHEME = "PassthroughXDB"
_ADMIN_TOKEN_SCHEME = "AdminToken"
_ADMIN_OAUTH_SCHEME = "AdminOAuth2"


def _dev_security_components(*, jwt_enabled: bool) -> dict[str, Any]:
    """Security schemes the dev surface declares.

    Three schemes total, all optional and any-of:

    - ``PassthroughBasic`` — standard ``http: basic`` on the Authorization
      header. Swagger UI gives username/password fields and auto-encodes,
      so the simple case (no JWT) gets the native experience. Only
      reachable when the deployment doesn't require JWT auth — when JWT
      is configured, the Authorization header is reserved for Bearer.
    - ``PassthroughXDB`` — the explicit ``X-DB-Authorization`` header form.
      Used when callers want to combine JWT (on Authorization) with DB
      passthrough on a separate header. Pasting the encoded
      ``Basic <b64>`` value is required because OpenAPI's apiKey scheme
      doesn't get the basic-auth field UX.
    - ``JWT`` — only declared when ``AUTH__JWKS_URL`` is configured; the
      spec stays honest about what the deployment validates.
    """
    schemes: dict[str, Any] = {
        _PASSTHROUGH_BASIC_SCHEME: {
            "type": "http",
            "scheme": "basic",
            "description": (
                "**Per-call DB credential passthrough.** When supplied, "
                "the service opens the database connection as the "
                "credential's user instead of the configured service "
                "account, so the DB's own GRANT / REVOKE and row-level "
                "security apply per call. Optional — omit to use the "
                "service account.\n\n"
                "Swagger UI's username/password fields auto-encode the "
                "value into `Authorization: Basic <b64>`. Only meaningful "
                "in deployments without JWT auth — when `AUTH__JWKS_URL` "
                "is configured, the Authorization header is reserved for "
                "Bearer tokens; use the **PassthroughXDB** scheme instead."
            ),
        },
        _PASSTHROUGH_XDB_SCHEME: {
            "type": "apiKey",
            "in": "header",
            "name": "X-DB-Authorization",
            "description": (
                "**Per-call DB credential passthrough on a separate "
                "header.** Use this when you also need to send a JWT on "
                "Authorization (i.e., JWT for API-level identity + DB "
                "creds for DB-level identity, on the same call). Most "
                "deployments don't need this — pick **PassthroughBasic** "
                "for the friendlier username/password dialog.\n\n"
                "Value format: paste `Basic <base64(user:pass)>` "
                "verbatim. Swagger UI doesn't auto-encode apiKey values "
                "on custom headers."
            ),
        },
    }
    if jwt_enabled:
        schemes[_JWT_SCHEME] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "User-identity JWT validated against `AUTH__JWKS_URL`.",
        }
    return schemes


def _dev_security_requirement(*, jwt_enabled: bool) -> list[dict[str, list[str]]]:
    """Per-operation security block for dev endpoints.

    Each item is an alternative. Order signals preferred path:
    PassthroughBasic first (Swagger-native UX), then the X-DB-Authorization
    form, then JWT when configured, then the un-authenticated escape
    when JWT isn't required.
    """
    requirements: list[dict[str, list[str]]] = [
        {_PASSTHROUGH_BASIC_SCHEME: []},
        {_PASSTHROUGH_XDB_SCHEME: []},
    ]
    if jwt_enabled:
        requirements.append({_JWT_SCHEME: []})
        requirements.append({_JWT_SCHEME: [], _PASSTHROUGH_XDB_SCHEME: []})
    else:
        requirements.append({})
    return requirements


def admin_security_components(settings: Any = None) -> dict[str, Any]:
    """Security scheme(s) for the admin sub-app's OpenAPI.

    Always emits the ``X-Admin-Token`` apiKey scheme. When ``settings``
    is supplied and all three OIDC URLs (`auth_url`, `token_url`,
    `client_id`) are configured, also emits an ``oauth2`` scheme so
    Swagger UI gets a working Authorize button that runs the OIDC
    popup against the IDP — operators without a gateway can sign in
    interactively rather than copy-pasting a JWT into the Bearer
    field. Either or both schemes pass the dispatcher gate.
    """
    components: dict[str, Any] = {
        _ADMIN_TOKEN_SCHEME: {
            "type": "apiKey",
            "in": "header",
            "name": "X-Admin-Token",
            "description": (
                "Shared-secret admin token. Configured via "
                "`AUTH__ADMIN_TOKEN` env. Endpoints fail closed (401) when "
                "the variant has no token configured."
            ),
        }
    }
    if settings is None:
        return components
    auth_cfg = settings.auth
    if auth_cfg.oidc_auth_url and auth_cfg.oidc_token_url and auth_cfg.oidc_client_id:
        components[_ADMIN_OAUTH_SCHEME] = {
            "type": "oauth2",
            "description": (
                "OAuth2 / OIDC sign-in. The Authorize button runs the "
                "browser popup against the configured IDP; the resulting "
                "access token is sent as a Bearer Authorization header. "
                "Requires a JWT with the configured admin claim "
                f"(`{auth_cfg.admin_claim_name}` includes `{auth_cfg.admin_group}`)."
            ),
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": auth_cfg.oidc_auth_url,
                    "tokenUrl": auth_cfg.oidc_token_url,
                    "scopes": {},
                }
            },
        }
    return components


def admin_security_requirement(settings: Any = None) -> list[dict[str, list[str]]]:
    """Per-operation security requirement for admin endpoints.

    Lists alternatives — either ``X-Admin-Token`` or (when configured)
    the OAuth2 scheme satisfies the gate.
    """
    requirements: list[dict[str, list[str]]] = [{_ADMIN_TOKEN_SCHEME: []}]
    if (
        settings is not None
        and settings.auth.oidc_auth_url
        and settings.auth.oidc_token_url
        and settings.auth.oidc_client_id
    ):
        requirements.append({_ADMIN_OAUTH_SCHEME: []})
    return requirements


def build_admin_openapi(
    admin_app: FastAPI,
    *,
    title: str,
    version: str,
    settings: Any = None,
) -> dict[str, Any]:
    """Override for ``admin_app.openapi`` that injects the admin
    security scheme(s) + per-operation requirement.

    Admin endpoints route through the ``verify_admin_access``
    dispatcher, so the spec must list both alternatives (token + JWT)
    when both are configured. This helper decorates the standard
    generated spec so Swagger UI shows an "Authorize" button covering
    every path the dispatcher accepts.
    """
    if admin_app.openapi_schema:
        return admin_app.openapi_schema

    schema = get_openapi(title=title, version=version, routes=admin_app.routes)
    components: dict[str, Any] = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update(admin_security_components(settings))
    requirement = admin_security_requirement(settings)
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict) and "operationId" in operation:
                operation["security"] = list(requirement)
    admin_app.openapi_schema = schema
    return schema


def _common_query_params(columns: list[str]) -> list[dict[str, Any]]:
    """Closed-grammar params, derived from
    :func:`core.engine.query_params.openapi_dicts` so the spec
    description, aliases, and constraints stay in lockstep with the
    FastAPI dependency at the route layer. ``columns`` is unused
    here today — operation descriptions carry the column list — but
    the parameter is retained for callers that might add per-column
    nuance later."""
    return closed_grammar_openapi_dicts()


def _allowed_filter_columns(
    columns: list[str],
    required: set,
    optional: set,
) -> set:
    """Set of columns valid for simple-filter at runtime.

    With a view def: narrow to required + optional. Without:
    every column. Mirrors the runtime validation in
    ``handlers/inventory._validate_simple_filter_columns``.
    """
    if required or optional:
        return (required | optional) & {c.lower() for c in columns}
    return {c.lower() for c in columns}


def _concrete_simple_filter_param(col: str, required: set) -> dict[str, Any]:
    """One concrete OpenAPI param entry for column ``col``.

    View-def-required columns are NOT marked ``required: true`` here:
    the runtime accepts either this simple filter OR an equality
    constraint in ``$filter``. Marking the simple filter
    required would make Swagger UI's client-side validation reject the
    ``$filter`` form. The description carries the contract instead.
    """
    return {
        "name": col,
        "in": "query",
        "required": False,
        "schema": {"type": "string"},
        "description": (
            f"Simple equality filter on `{col}`."
            + (
                f" **Required by view def** — supply this directly "
                f"OR constrain via `$filter` (e.g. `$filter={col} eq <value>`)."
                if col in required
                else ""
            )
        ),
    }


def _generic_simple_filter_param(allowed_cols: list[str], required: set) -> dict[str, Any]:
    """Single 'any column' simple-filter param used by simple-schema and
    by optimized-schema's non-indexed-column tail. The actual parameter
    name a caller uses isn't ``filter`` — they use whatever column they
    want. This entry exists in the spec to document that simple
    equality filters work on the listed columns.

    The column list isn't repeated here — it lives in the operation
    description. View-def required columns *are* called out inline
    because the constraint is per-entry semantic.
    """
    required_note = (
        f" Required by view def: {', '.join(sorted(required))}."
        " Supply via simple filter (?col=val) OR equality-constrained `$filter`."
        if required
        else ""
    )
    return {
        "name": "<any-listed-column>",
        "in": "query",
        "required": False,
        "schema": {"type": "string"},
        "description": (
            "Simple equality filter — the query param name is the column to filter on. "
            "Allowed columns are listed in the operation description "
            "(or the view def's required/optional columns when set)."
            f"{required_note} "
            "For multiple columns or non-equality predicates, use `$filter`."
        ),
    }


def _simple_filter_params(
    columns: list[str],
    required: set,
    optional: set,
    *,
    mode: str = "simple-schema",
    indexed_cols: set | None = None,
) -> list[dict[str, Any]]:
    """Build the simple-filter params for one table's operation.

    ``mode`` controls verbosity:

    - ``simple-schema`` (default): one generic ``<any-listed-column>``
      entry. Spec is O(1) per table regardless of column count.
    - ``optimized-schema``: concrete params for indexed/PK/FK columns
      (``indexed_cols``); non-indexed columns surface via the same
      generic entry as simple-schema. Falls back to simple-schema
      layout when ``indexed_cols`` is empty (e.g., Trino, or a table
      with no usable indexes).
    - ``full-schema``: concrete params for every allowed column.
      previously behavior preserved as the legacy / dev escape.

    Runtime accepts any allowed column regardless of mode — the spec
    only differs in how it documents the surface.
    """
    allowed = _allowed_filter_columns(columns, required, optional)
    sorted_allowed = sorted(allowed)

    if mode == "full-schema":
        return [_concrete_simple_filter_param(col, required) for col in sorted_allowed]

    if mode == "optimized-schema":
        indexed = (indexed_cols or set()) & allowed
        if not indexed:
            # No usable index info; degrade gracefully to simple-schema.
            return [_generic_simple_filter_param(sorted_allowed, required)]
        params = [_concrete_simple_filter_param(col, required) for col in sorted(indexed)]
        non_indexed_tail = allowed - indexed
        if non_indexed_tail:
            params.append(_generic_simple_filter_param(sorted(non_indexed_tail), required))
        return params

    # simple-schema (default)
    return [_generic_simple_filter_param(sorted_allowed, required)]


def _result_response(*, summary: str, include_links: bool) -> dict[str, Any]:
    """Standard 200 response shape for row-returning endpoints."""
    properties: dict[str, Any] = {
        "name": {"type": "string"},
        "elements": {"type": "array", "items": {"type": "object"}},
    }
    if include_links:
        properties["links"] = {"type": "array", "items": {"type": "object"}}
    return {
        "description": summary,
        "content": {"application/json": {"schema": {"type": "object", "properties": properties}}},
    }


def _operation(
    *,
    tags: list[str],
    summary: str,
    description_parts: list[str],
    parameters: list[dict[str, Any]],
    success_summary: str,
    not_found_summary: str,
    validation_summary: str,
    include_links: bool,
) -> dict[str, Any]:
    """Common GET operation envelope for table + custom-query entries."""
    return {
        "get": {
            "tags": tags,
            "summary": summary,
            "description": "\n\n".join(description_parts),
            "parameters": parameters,
            "responses": {
                "200": _result_response(summary=success_summary, include_links=include_links),
                "400": {"description": validation_summary},
                "404": {"description": not_found_summary},
                "503": {"description": "Service temporarily unavailable (circuit breaker open)."},
            },
        }
    }


def _table_operation(
    schema_name: str,
    table_name: str,
    columns: list[str],
    view_def_required: set,
    view_def_optional: set,
    aliases: list[str] | None = None,
    *,
    render_mode: str = "simple-schema",
    indexed_cols: set | None = None,
) -> dict[str, Any]:
    """One GET operation entry for ``/{schema}/{table}``.

    Tagged with the schema name so Swagger UI collapses tables under
    one group per schema — keeps the surface scannable on deployments
    with thousands of tables across multiple schemas.

    ``aliases``, when supplied, are listed in the operation
    description so callers see that legacy URL forms also reach this
    handler.

    ``render_mode`` controls per-column simple-filter verbosity (see
    ``_simple_filter_params``); ``indexed_cols`` is the set of columns
    optimized-schema mode surfaces as concrete params.
    """
    description_parts = [
        f"Query the `{schema_name}.{table_name}` table.",
        f"Columns: `{', '.join(columns)}`.",
    ]
    if view_def_required:
        description_parts.append(
            f"View def requires: `{', '.join(sorted(view_def_required))}` "
            "(via simple filter or equality-constrained `$filter`)."
        )
    if view_def_optional:
        description_parts.append(f"View def optional: `{', '.join(sorted(view_def_optional))}`.")
    if render_mode == "optimized-schema" and indexed_cols:
        description_parts.append(
            f"**Indexed / key columns surfaced as autocomplete-able params**: "
            f"`{', '.join(sorted(indexed_cols))}`. Other columns are still "
            "filterable via `$filter` or as simple `?col=val` query params."
        )
    if aliases:
        description_parts.append(
            "**Also reachable at**: "
            + ", ".join(f"`{a}`" for a in sorted(aliases))
            + " — legacy URL aliases declared in the YAML."
        )

    return _operation(
        tags=[f"data: {schema_name}"],
        summary=f"Query {schema_name}.{table_name}",
        description_parts=description_parts,
        parameters=(
            _common_query_params(columns)
            + _simple_filter_params(
                columns,
                view_def_required,
                view_def_optional,
                mode=render_mode,
                indexed_cols=indexed_cols,
            )
        ),
        success_summary="Result rows.",
        not_found_summary="Schema or table not found.",
        validation_summary="Validation error.",
        include_links=True,
    )


def _query_tag(path: str) -> str:
    """Group custom queries by their first path segment so a YAML tree
    like ``queries/reports/...`` and ``queries/analytics/...`` collapses
    into per-folder groups in Swagger UI. Queries at the queries root
    fall back to plain ``queries``.
    """
    head, _, _ = path.partition("/")
    return f"queries: {head}" if head and "/" in path else "queries"


def _query_operation(
    path: str,
    name: str,
    required: set,
    optional: set,
    aliases: list[str] | None = None,
) -> dict[str, Any]:
    """One GET operation entry for ``/queries/<path>``."""
    parameters: list[dict[str, Any]] = []
    for col in sorted(required | optional):
        parameters.append(
            {
                "name": col,
                "in": "query",
                "required": col in required,
                "schema": {"type": "string"},
                "description": (
                    f"Bound to `:{col}` in the YAML SQL."
                    + (" **Required.**" if col in required else "")
                ),
            }
        )

    description_parts = [f"Run the operator-authored `{name}` query."]
    if required:
        description_parts.append(f"Required params: `{', '.join(sorted(required))}`.")
    if optional:
        description_parts.append(f"Optional params: `{', '.join(sorted(optional))}`.")
    if not (required or optional):
        description_parts.append("No parameters declared in YAML.")
    if aliases:
        description_parts.append(
            "**Also reachable at**: "
            + ", ".join(f"`{a}`" for a in sorted(aliases))
            + " — legacy URL aliases declared in the YAML."
        )

    return _operation(
        tags=[_query_tag(path)],
        summary=f"Custom query: {name}",
        description_parts=description_parts,
        parameters=parameters,
        success_summary="Query result rows.",
        not_found_summary="Query not registered.",
        validation_summary="Missing or unknown parameter.",
        include_links=False,
    )


def build_dev_openapi(
    app: FastAPI,
    *,
    title: str,
    version: str,
    jwt_enabled: bool = False,
    render_mode: str = "simple-schema",
    allowlist_mode: str = "enforce",
) -> dict[str, Any]:
    """Synthesize the per-deployment dev OpenAPI spec.

    Starts from the standard FastAPI-generated spec (``/health``,
    ``/ready``, ``/readyz``) and injects one operation per harvested
    table and one per registered custom query. The generic
    ``/{schema}/{view_name}`` and ``/queries/{path:path}`` routes are
    hidden via ``include_in_schema=False``, so only the concrete
    entries appear.

    Also declares security schemes (passthrough Basic always; JWT when
    ``jwt_enabled`` is true) so Swagger UI's "Authorize" dialog is
    wired up. Only data-and-query operations carry the security
    requirement; ``/health``, ``/ready``, ``/readyz`` stay open.

    ``allowlist_mode="presentation"`` makes the allowlist a spec-only
    filter — schemas / tables not matching the loaded ``allowlist.yaml``
    are skipped here, while remaining reachable at runtime via the
    full DDL cache.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(title=title, version=version, routes=app.routes)
    paths: dict[str, Any] = schema.setdefault("paths", {})

    components: dict[str, Any] = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update(
        _dev_security_components(jwt_enabled=jwt_enabled),
    )
    dev_security = _dev_security_requirement(jwt_enabled=jwt_enabled)

    # In presentation mode, fetch the loaded allowlist so the renderer
    # can filter — the cache itself is unfiltered in that mode.
    presentation_filter = None
    if allowlist_mode == "presentation":
        from .loaders import allowlist as _allowlist

        loaded = _allowlist.get()
        if not loaded.is_empty():
            presentation_filter = loaded

    # Track schemas/tables actually rendered in the spec — drives the
    # tags array below and stays consistent with presentation-mode
    # filtering (a schema dropped from paths shouldn't appear in tags).
    rendered_schema_table_counts: dict[str, int] = {}
    for entry in iter_tables():
        if presentation_filter is not None and not presentation_filter.schema_allowed(entry.schema):
            continue
        if presentation_filter is not None and not presentation_filter.table_allowed(
            entry.schema, entry.table
        ):
            continue
        required = entry.view_def.required if entry.view_def else set()
        optional = entry.view_def.optional if entry.view_def else set()
        target_path = f"/{entry.schema}/{entry.table}"
        op = _table_operation(
            entry.schema,
            entry.table,
            sorted(entry.columns),
            required,
            optional,
            aliases=alias_routes.get_accepted_aliases(target_path),
            render_mode=render_mode,
            indexed_cols=entry.indexed_columns,
        )
        op["get"]["security"] = list(dev_security)
        paths[target_path] = op
        rendered_schema_table_counts[entry.schema] = (
            rendered_schema_table_counts.get(entry.schema, 0) + 1
        )

    query_paths: list[str] = []
    for q in iter_queries():
        target_path = f"/queries/{q.path}"
        op = _query_operation(
            q.path,
            q.qdef.name,
            q.qdef.view_def.required,
            q.qdef.view_def.optional,
            aliases=alias_routes.get_accepted_aliases(target_path),
        )
        op["get"]["security"] = list(dev_security)
        paths[target_path] = op
        query_paths.append(q.path)

    # Top-level tags array drives Swagger UI's group ordering and
    # provides a description per group. Schema groups come first
    # (alphabetical), then query-folder groups, then any pre-existing
    # tags get-openapi already populated for /health, /ready, etc.
    existing_tags = {t["name"]: t for t in schema.get("tags", [])}
    schema_tags: list[dict[str, Any]] = []
    for schema_name in sorted(rendered_schema_table_counts):
        table_count = rendered_schema_table_counts[schema_name]
        schema_tags.append(
            {
                "name": f"data: {schema_name}",
                "description": f"Tables in `{schema_name}` schema ({table_count}).",
            }
        )
    query_groups: dict[str, int] = {}
    for path in query_paths:
        query_groups[_query_tag(path)] = query_groups.get(_query_tag(path), 0) + 1
    for tag_name in sorted(query_groups.keys()):
        schema_tags.append(
            {
                "name": tag_name,
                "description": f"Operator-authored custom queries ({query_groups[tag_name]}).",
            }
        )
    schema["tags"] = schema_tags + list(existing_tags.values())

    app.openapi_schema = schema
    return schema


def invalidate(app: FastAPI) -> None:
    """Clear the cached spec so the next ``/openapi.json`` regenerates.

    Called by ``/admin/refresh-schema`` (after DDL re-harvest) and
    ``/admin/reload-config`` (after view-def + custom-query reload).
    """
    app.openapi_schema = None
