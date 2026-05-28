"""Alias route registration for legacy-gateway migration.

Operators migrating from a legacy URL surface (legacy data-virtualization layers, custom internal
proxies, vendor REST APIs being replaced) want the service to also respond at
the legacy URL paths, so the gateway swap is mechanically transparent
to existing consumers.

Aliases live in the validation YAMLs (one list per ``/{schema}/{table}``)
and the custom-query YAMLs (one list per ``/queries/<path>``). At
lifespan startup, after the loaders run, this module:

1. Validates each alias against a reserved-prefix list — paths the service
   owns (``/health``, ``/ready``, ``/admin/*``, ``/queries/*``,
   ``/openapi.json``, ``/docs``, ``/redoc``) cannot be aliased over.
2. Detects collisions among aliases (two YAMLs declaring the same
   alias) and refuses the second — first-wins keeps behavior
   deterministic.
3. Warns when an alias shadows a real ``/{schema}/{table}`` route in
   the harvested DDL cache (the alias takes precedence; the canonical
   route is unreachable). Doesn't reject — operators may legitimately
   want this.
4. Registers one FastAPI route per accepted alias delegating to the
   canonical handler. Query parameters pass through unchanged.

The accepted-aliases-by-target dict is the single source of truth
consumed by both route registration and the OpenAPI spec generator —
ensures the spec describes only aliases that actually route, never
ones that were rejected at load time.

v1 scope: aliases register at lifespan startup only. Live reload via
``/admin/reload-config`` is not supported; alias changes require a
pod restart.
"""

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Request

from .engine import schema_cache
from .engine.query_params import ClosedGrammarParams
from .handlers.inventory import query_view_impl
from .loaders import queries as custom_queries
from .loaders import validation as view_defs

logger = logging.getLogger(__name__)

_RESERVED_PREFIXES = (
    "/health",
    "/ready",
    "/readyz",
    "/admin",
    "/queries",
    "/openapi.json",
    "/docs",
    "/redoc",
)


@dataclass(frozen=True)
class _TableTarget:
    """The alias should route to ``/<schema>/<table>``."""

    schema: str
    table: str

    @property
    def path(self) -> str:
        return f"/{self.schema}/{self.table}"


@dataclass(frozen=True)
class _QueryTarget:
    """The alias should route to ``/queries/<query_path>``."""

    query_path: str

    @property
    def path(self) -> str:
        return f"/queries/{self.query_path}"


_Target = _TableTarget | _QueryTarget


# Populated by register_all; consulted by get_accepted_aliases. Maps
# canonical target path → list of accepted aliases. Empty until
# register_all runs.
_accepted_by_target: dict[str, list[str]] = {}


def _is_reserved(alias: str) -> str | None:
    """Return a non-empty rejection message if ``alias`` collides with
    a path the service owns; ``None`` when the path is acceptable."""
    if not alias.startswith("/"):
        return f"alias {alias!r} must start with '/'"
    for prefix in _RESERVED_PREFIXES:
        if alias == prefix or alias.startswith(prefix + "/"):
            return f"alias {alias!r} collides with reserved path {prefix!r}"
    return None


def _shadows_dynamic_route(alias: str) -> tuple[str, str] | None:
    """If ``alias`` looks like ``/<schema>/<table>`` AND the table
    exists in the harvested DDL cache, return ``(schema, table)`` —
    caller logs a warning. The alias still registers; alias takes
    precedence."""
    parts = alias.strip("/").split("/")
    if len(parts) != 2:
        return None
    schema, table = parts[0], parts[1]
    if schema_cache.validate_table(schema, table) is None:
        return (schema.lower(), table.lower())
    return None


def _collect_aliases() -> tuple[
    list[tuple[str, _Target, str]],  # accepted (origin, target, alias)
    list[tuple[str, str, str]],  # rejected (origin, target_path, reason)
]:
    """Walk both YAML registries and split aliases into accepted /
    rejected piles.

    ``origin`` is a human-readable source (``view_def:public.products``,
    ``query:reports/by_category``) for log clarity. The accepted list
    carries a tagged ``_Target`` so callers don't have to re-parse the
    target path.
    """
    accepted: list[tuple[str, _Target, str]] = []
    rejected: list[tuple[str, str, str]] = []
    seen: dict = {}  # alias path → first origin that claimed it

    for schema, table, alias in view_defs.all_aliases():
        origin = f"view_def:{schema}.{table}"
        target: _Target = _TableTarget(schema=schema, table=table)
        why = _is_reserved(alias)
        if why:
            rejected.append((origin, target.path, why))
            continue
        if alias in seen:
            rejected.append(
                (origin, target.path, f"alias {alias!r} already declared by {seen[alias]!r}")
            )
            continue
        seen[alias] = origin
        accepted.append((origin, target, alias))

    for path in custom_queries.list_queries():
        qdef = custom_queries.get_query(path)
        if qdef is None or not qdef.aliases:
            continue
        origin = f"query:{path}"
        target = _QueryTarget(query_path=path)
        for alias in qdef.aliases:
            why = _is_reserved(alias)
            if why:
                rejected.append((origin, target.path, why))
                continue
            if alias in seen:
                rejected.append(
                    (origin, target.path, f"alias {alias!r} already declared by {seen[alias]!r}")
                )
                continue
            seen[alias] = origin
            accepted.append((origin, target, alias))

    return accepted, rejected


def _add_route_at_front(app: FastAPI, alias: str, handler) -> None:
    """Add a route and move it to the front of the route list.

    FastAPI's ``app.add_api_route`` appends, but the dynamic
    ``/{schema}/{view_name}`` and ``/queries/{path:path}`` catch-alls
    are already registered. Anything appended after them never matches
    when the alias has two or three segments respectively. Moving the
    new route to the front gives aliases priority — desired since the
    operator's intent in declaring an alias is "this URL should reach
    this specific handler regardless of what would otherwise catch it."
    """
    app.add_api_route(
        alias,
        handler,
        methods=["GET"],
        include_in_schema=False,
        tags=["aliases"],
    )
    new_route = app.router.routes.pop()
    app.router.routes.insert(0, new_route)


def _register_table_alias(app: FastAPI, alias: str, schema: str, table: str) -> None:
    """Add a FastAPI route at ``alias`` that delegates to
    :func:`query_view_impl` with the canonical schema/table."""

    async def _alias_handler(
        request: Request,
        params: Annotated[ClosedGrammarParams, Depends()],
    ):
        return await query_view_impl(
            request,
            schema,
            table,
            params.select,
            params.filter_,
            params.orderby,
            params.count,
            params.start_index,
            params.groupby,
            params.having,
            catalog=None,
            cursor=params.cursor,
        )

    _add_route_at_front(app, alias, _alias_handler)


def _register_query_alias(app: FastAPI, alias: str, query_path: str) -> None:
    """Add a FastAPI route at ``alias`` that delegates to the same
    custom-query execution path as ``/queries/<query_path>``."""
    # Lazy import to avoid pulling in route handlers at module-load time
    # (which would trigger auth/etc. circular imports during early init).
    from .routes.queries import run_query

    async def _alias_handler(request: Request):
        return await run_query(request, query_path)

    _add_route_at_front(app, alias, _alias_handler)


def register_all(app: FastAPI) -> int:
    """Register every accepted alias as a FastAPI route on ``app``.

    Populates ``_accepted_by_target`` so the OpenAPI spec generator
    can describe only aliases that actually route. Returns the count
    of registered aliases.
    """
    global _accepted_by_target
    _accepted_by_target = {}
    accepted, rejected = _collect_aliases()

    for origin, target_path, reason in rejected:
        logger.error(
            "Alias rejected — origin=%s target=%s reason: %s",
            origin,
            target_path,
            reason,
        )

    for origin, target, alias in accepted:
        shadowed = _shadows_dynamic_route(alias)
        if shadowed:
            sh_schema, sh_table = shadowed
            logger.warning(
                "Alias %s shadows dynamic route /%s/%s — alias takes precedence; "
                "the canonical table route at that path is unreachable.",
                alias,
                sh_schema,
                sh_table,
            )

        if isinstance(target, _QueryTarget):
            _register_query_alias(app, alias, target.query_path)
        else:
            _register_table_alias(app, alias, target.schema, target.table)

        _accepted_by_target.setdefault(target.path, []).append(alias)
        logger.info(
            "Registered alias %s → %s (origin=%s)",
            alias,
            target.path,
            origin,
        )

    return len(accepted)


def get_accepted_aliases(target_path: str) -> list[str]:
    """Return the aliases that successfully registered for a target
    path (``/<schema>/<table>`` or ``/queries/<path>``).

    Used by the dynamic OpenAPI spec to describe only aliases that
    actually route — rejected aliases (reserved-prefix collisions,
    inter-alias collisions) are absent from this dict so the spec
    never lies about what's reachable.
    """
    return list(_accepted_by_target.get(target_path, []))
