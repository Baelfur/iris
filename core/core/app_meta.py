"""FastAPI app construction shared across all variants.

``build_app(...)`` packages the lifespan + middleware + router wiring
that every variant's ``app/main.py`` was duplicating. Variants now
become ~10-line files supplying just the per-variant deltas: database
tag, paramstyle, db module, the user attribute name on settings, and
optionally an ``extra_routes`` callback for variants like Trino that
register additional routes.

The helpers ``make_app_title`` and ``build_app`` both live here because
they're the small "FastAPI app metadata" surface that variants poke at
during startup.
"""

import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

from core import aliases as alias_routes
from core import openapi_dynamic, startup_warnings
from core.auth.admin import verify_admin_access
from core.config import source as _config_source
from core.config.settings import app_header_slug
from core.context import AppContext, set_context
from core.engine import circuit_breaker, pool_sizing, schema_cache
from core.errors import handler as error_handler
from core.loaders import allowlist
from core.loaders import queries as custom_queries
from core.loaders import validation as view_defs
from core.observability import metrics, tracing
from core.observability.logging_config import setup_logging
from core.routes.admin import router as admin_router
from core.routes.inventory import router as inventory_router
from core.routes.queries import admin_router as queries_admin_router
from core.routes.queries import router as queries_router
from core.routes.readiness import router as readiness_router


def make_app_title(app_name: str, deployment_name: str = "") -> str:
    """OpenAPI title for the FastAPI app.

    Uses ``APP_NAME`` for the brand component (operator-configurable per
    — defaults to ``"app"`` but commonly set to the surrounding
    product's brand like ``"iris"`` or ``"resource-direct"``). When
    ``DEPLOYMENT_NAME`` is set, the title suffixes it so ``/docs`` and
    ``/openapi.json`` identify the specific deployment — useful in
    fleets where multiple instances have similarly-shaped Swagger
    pages fronting different DBs.
    """
    slug = app_header_slug(app_name)
    return f"{slug} — {deployment_name}" if deployment_name else slug


def build_app(
    *,
    database: str,
    paramstyle: Literal["pyformat", "named", "qmark"],
    settings: Any,
    db_module: Any,
    db_user: str,
    extra_routes: Callable[[FastAPI, Any], None] | None = None,
) -> FastAPI:
    """Build the variant's FastAPI application.

    Args:
        database: Tag for the database family ("postgresql", "mysql",
            "mariadb", "oracle", "trino"). Cascades into log records
            and the AppContext.
        paramstyle: SQL placeholder style — ``"pyformat"`` |
            ``"named"`` | ``"qmark"``. Drives ``build_query``'s
            placeholder + binds shape.
        settings: The variant's instantiated ``Settings`` object.
            Must expose the shared ``AppSettings`` fields plus
            whatever per-variant connection vars its ``db_module``
            consumes.
        db_module: The variant's ``app.db`` module. Required attrs:
            ``init_pool``, ``close_pool``, ``fetch_all``,
            ``harvest_ddl``, ``ping``, ``fetch_all_with_creds``,
            ``get_connection_limit``.
        db_user: The DB-user value to expose on ``AppContext.db_user``
            (used by passthrough error logs). Variant supplies its own
            settings field — ``settings.pg_user``,
            ``settings.mysql_user``, etc.
        extra_routes: Optional callback ``(app, settings)`` invoked
            after the standard routers are registered and before the
            error handler / health endpoint / metrics. Variants like
            Trino use this to register additional endpoints.

    Returns the configured FastAPI app. The variant's ``main.py``
    typically just assigns this to module-level ``app`` so uvicorn
    can find ``app.main:app``.
    """
    setup_logging(
        database=database,
        app_name=settings.app_name,
        deployment_name=settings.deployment_name,
        kafka_settings=settings,
        log_level=settings.log_level,
    )
    logger = logging.getLogger(__name__)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """FastAPI lifespan: pool open → config materialize → DDL harvest
        → allowlist narrow → metrics/tracing/Kafka attach → ready.

        Yields once after all setup completes; the inverse runs on the
        teardown side (Kafka flush, pool close). Failures during setup
        propagate and abort startup — the service never serves traffic
        in a half-initialized state.
        """
        startup_warnings.report(settings)
        logger.info("Starting connection pool")
        await db_module.init_pool()

        cfg = _config_source.from_settings(settings)
        cfg_root = cfg.materialize()
        ctx = AppContext(
            fetch_all=db_module.fetch_all,
            harvest_ddl=db_module.harvest_ddl,
            paramstyle=paramstyle,
            settings=settings,
            database=database,
            fetch_all_with_creds=db_module.fetch_all_with_creds,
            ping=db_module.ping,
            get_connection_limit=db_module.get_connection_limit,
            breaker=circuit_breaker.from_settings(settings),
            config_source=cfg,
            db_user=db_user,
        )
        set_context(ctx)

        logger.info("Harvesting DDL")
        count = await schema_cache.refresh()
        logger.info("DDL harvest complete: %d tables cached", count)

        # Allowlist narrows the cache by schema + table globs.
        # No-op when allowlist.yaml is missing or empty — preserves
        # today's open-by-default posture. ALLOWLIST__MODE=presentation
        # makes this a no-op for the cache; the spec renderer
        # applies the allowlist at render time instead.
        allowlist.load(str(cfg_root))
        allowlist.narrow_cache(schema_cache.get_cache(), mode=settings.allowlist_mode)

        startup_warnings.report_post_harvest(settings)

        vcount = view_defs.load_views(str(cfg_root / "validation"))
        if vcount:
            logger.info("Loaded %d view definition(s)", vcount)
            view_defs.warn_mismatches(schema_cache.get_cache())

        qcount = custom_queries.load_queries(str(cfg_root / "queries"))
        if qcount:
            logger.info("Loaded %d custom query/queries", qcount)

        # Register alias routes from the loaded YAMLs. Live alias
        # changes via /admin/reload-config are not supported in v1 —
        # alias additions/removals require a process restart.
        acount = alias_routes.register_all(app)
        if acount:
            logger.info("Registered %d alias route(s)", acount)

        await pool_sizing.report_pool_sizing(ctx)

        yield
        logger.info("Closing connection pool")
        await db_module.close_pool()

    app_title = make_app_title(settings.app_name, settings.deployment_name)
    # `enabled` (default) preserves today's behavior — /docs, /redoc,
    # and /openapi.json are all reachable unauthenticated. `admin-enabled`
    # disables the HTML pages and remounts /openapi.json behind the admin
    # token (see below where the custom route is registered). `disabled`
    # turns all three off.
    if settings.openapi_visibility == "enabled":
        app = FastAPI(title=app_title, lifespan=lifespan)
    else:
        # admin-enabled and disabled both shut off the standard
        # FastAPI-mounted endpoints; admin-enabled then re-adds the
        # JSON endpoint under the admin-token gate.
        app = FastAPI(
            title=app_title,
            lifespan=lifespan,
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
    # Admin sub-app gates every route on the verify_admin_access
    # dispatcher at the FastAPI dependency layer — defense-in-depth so
    # a future endpoint that forgets the per-handler call can't
    # silently expose itself. The dispatcher accepts either
    # X-Admin-Token (existing path) or a Bearer JWT with the configured
    # admin claim. Per-handler calls remain as belt-and-braces.
    #
    #
    # Default `docs_url` / `redoc_url` / `openapi_url` are disabled and
    # re-registered below as decorator-added routes so they inherit the
    # sub-app's `dependencies=[Depends(verify_admin_access)]`. FastAPI's
    # auto-mounted spec/docs endpoints use `add_route` (not
    # `add_api_route`), bypassing router-level dependencies — so without
    # this re-mounting, /admin/openapi.json and /admin/docs would be
    # publicly accessible despite the sub-app dependency.
    admin_app = FastAPI(
        title=f"{app_title} — Admin",
        dependencies=[Depends(verify_admin_access)],
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    @admin_app.get("/openapi.json", include_in_schema=False)
    async def _admin_openapi():
        return admin_app.openapi()

    @admin_app.get("/docs", include_in_schema=False)
    async def _admin_docs():
        # Pre-fill the OIDC client_id in the Swagger Authorize popup
        # when the operator has configured the OAuth2 metadata.
        # When unset, the kwargs collapse to today's behavior — a plain
        # Authorize dialog asking for the Bearer token manually.
        init_oauth = (
            {"clientId": settings.auth.oidc_client_id, "usePkceWithAuthorizationCodeGrant": True}
            if settings.auth.oidc_client_id
            else None
        )
        return get_swagger_ui_html(
            openapi_url="/admin/openapi.json",
            title=admin_app.title,
            init_oauth=init_oauth,
        )

    @admin_app.get("/redoc", include_in_schema=False)
    async def _admin_redoc():
        return get_redoc_html(
            openapi_url="/admin/openapi.json",
            title=admin_app.title,
        )

    deployment_header = f"X-{app_header_slug(settings.app_name)}-Deployment"

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """Log every request with method, path, status, and duration. Also
               stamps a ``X-{AppName}-Deployment`` header on every response when
               DEPLOYMENT_NAME is set so multi-instance environments can identify
               which producer answered. The header name derives from APP_NAME
        — operator-branded; defaults to ``X-App-Deployment``.
        """
        start = time.perf_counter()
        response = await call_next(request)
        if settings.deployment_name:
            response.headers[deployment_header] = settings.deployment_name
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "%s %s %s %sms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    # Admin sub-app mounted at `/admin` — registered BEFORE the
    # dev-facing routers so its prefix matches before the generic
    # `/{schema}/{view_name}` (and Trino's 3-segment) route. Otherwise
    # `/admin/...` paths match `schema=admin` and never reach the
    # mount. The parent-app reference lets admin handlers invalidate
    # the dev OpenAPI cache after schema-affecting operations.
    admin_app.state.parent_app = app
    admin_app.include_router(admin_router)
    admin_app.include_router(queries_admin_router)
    error_handler.register(admin_app)
    app.mount("/admin", admin_app)

    # Dev-facing routers — surfaced in the per-deployment OpenAPI spec
    # consumers download. Concrete `/{schema}/{table}` and
    # `/queries/<path>` routes are injected at spec-generation time
    # by `openapi_dynamic.build_dev_openapi`.
    app.include_router(queries_router)
    app.include_router(readiness_router)
    app.include_router(inventory_router)

    if extra_routes is not None:
        extra_routes(app, settings)

    error_handler.register(app)

    # Per-deployment dynamic OpenAPI — `/openapi.json` enumerates concrete
    # routes per harvested table + custom query rather than the generic
    # `/{schema}/{view_name}` placeholder, and declares the security
    # schemes the deployment supports so Swagger UI wires up an
    # "Authorize" button (passthrough Basic always; JWT only when
    # `AUTH__JWKS_URL` is configured). Cached on the app; admin reload
    # endpoints invalidate via `openapi_dynamic.invalidate`.
    jwt_enabled = bool(settings.auth.jwks_url)
    # FastAPI's openapi() is documented as overridable but typed as a
    # bound method; mypy flags the assignment. Standard FastAPI pattern.
    app.openapi = lambda: openapi_dynamic.build_dev_openapi(  # type: ignore[method-assign]
        app,
        title=app_title,
        version=app.version,
        jwt_enabled=jwt_enabled,
        render_mode=settings.openapi_render_mode,
        allowlist_mode=settings.allowlist_mode,
    )
    admin_app.openapi = lambda: openapi_dynamic.build_admin_openapi(  # type: ignore[method-assign]
        admin_app,
        title=admin_app.title,
        version=admin_app.version,
        settings=settings,
    )

    # admin-enabled visibility: standard /openapi.json is off (openapi_url=None
    # above), so re-register it as a custom route protected by the admin
    # token. Programmatic spec consumers (SDK builds, doc generators) authenticate
    # with the same secret that gates /admin/*. The lambda still owns spec
    # generation + caching, so behavior matches the standard mount otherwise.
    if settings.openapi_visibility == "admin-enabled":

        @app.get("/openapi.json", dependencies=[Depends(verify_admin_access)])
        async def admin_gated_openapi():
            """Admin-gated /openapi.json for `admin-enabled` visibility.

            Returns the same spec the standard mount would emit; the
            `verify_admin_access` dispatcher rejects unauthenticated
            callers with 401. Accepts either ``X-Admin-Token`` or a
            Bearer JWT with the configured admin claim.
            """
            return app.openapi()

    @app.get("/health")
    async def health():
        """Liveness probe. Includes ``deployment`` when DEPLOYMENT_NAME is set
        so dashboards hitting probes can identify the specific instance."""
        payload = {"status": "ok"}
        if settings.deployment_name:
            payload["deployment"] = settings.deployment_name
        return payload

    # metrics + tracing must come AFTER include_router calls so the
    # instrumentator's per-route labels and OTel spans cover those routes.
    metrics.maybe_register(app, settings)
    tracing.maybe_register(app, settings)

    return app
