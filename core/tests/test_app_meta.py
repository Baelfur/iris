"""Tests for core.app_meta — title helper + build_app factory."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import context as _context
from core.app_meta import build_app, make_app_title
from core.config.settings import AuthSettings, ConfigSettings, AppSettings
from core.engine import schema_cache


class TestMakeAppTitle:
    def test_neutral_default(self):
        # Default APP_NAME is "app" (neutral) — title cases the kebab
        # segments via app_header_slug.
        assert make_app_title("app") == "App"
        assert make_app_title("app", "") == "App"

    def test_suffixed_when_deployment_set(self):
        assert make_app_title("app", "inventory") == "App — inventory"

    def test_operator_brand_passes_through(self):
        # Operators who want the historical "IRIS" caps set APP_NAME=IRIS;
        # the helper preserves operator-supplied caps.
        assert make_app_title("IRIS", "inventory") == "IRIS — inventory"

    def test_kebab_brand_title_cased(self):
        # Multi-segment kebab brand: each lowercase segment title-cased,
        # so the value drops cleanly into header names like
        # X-Resource-Direct-Deployment.
        assert make_app_title("resource-direct", "billing") == (
            "Resource-Direct — billing"
        )

    def test_no_special_handling_for_long_deployment_names(self):
        # Validation lives in AppSettings; the title helper trusts
        # whatever it gets. Long names just get the longer suffix.
        assert make_app_title("app", "a" * 63) == "App — " + ("a" * 63)


def _fake_db_module():
    """Minimal db module shape that satisfies build_app's lifespan.

    All hooks return empty/None so the lifespan walks every step
    without touching a real driver.
    """

    class _FakeDB:
        init_pool = AsyncMock()
        close_pool = AsyncMock()

        @staticmethod
        async def harvest_ddl():
            return {}

        @staticmethod
        async def fetch_all(sql, params=None):
            return []

        @staticmethod
        async def fetch_all_with_creds(sql, params, creds):
            return []

        @staticmethod
        async def ping():
            return None

        @staticmethod
        async def get_connection_limit():
            return (None, "fake")

    return _FakeDB()


def _settings(deployment_name: str = "") -> AppSettings:
    # require_passthrough=False because the spec-rendering tests hit
    # /openapi.json (fine either way) and the few that hit data routes
    # do so via a fake DB module without supplying creds.
    return AppSettings(
        deployment_name=deployment_name,
        auth=AuthSettings(mode="gateway", require_passthrough=False),
        config=ConfigSettings(source="local"),
    )


def _build(*, settings=None, db=None, **overrides) -> FastAPI:
    """Construct a build_app with sensible defaults plus per-test overrides."""
    return build_app(
        database="postgresql",
        paramstyle="pyformat",
        settings=settings if settings is not None else _settings(),
        db_module=db if db is not None else _fake_db_module(),
        db_user="app",
        **overrides,
    )


@pytest.fixture(autouse=True)
def _reset_global_state():
    """build_app's lifespan installs a process-global AppContext and
    populates schema_cache. Clear both before and after each test so
    cross-test leakage stays contained."""
    _context._ctx = None
    schema_cache._cache.clear()
    yield
    _context._ctx = None
    schema_cache._cache.clear()


class TestBuildApp:
    def test_registers_standard_routers(self):
        app = _build()
        assert isinstance(app, FastAPI)
        paths = {r.path for r in app.routes}
        # Dev-facing routes on the main app.
        assert "/health" in paths
        assert "/ready" in paths
        assert "/{schema}/{view_name}" in paths
        assert "/queries/{path:path}" in paths
        # The admin sub-app is mounted at /admin (#253). Its concrete
        # endpoints aren't in app.routes — they live on the mounted
        # FastAPI instance's own routes list.
        assert "/admin" in paths
        admin_app = next(
            r.app for r in app.routes if getattr(r, "path", None) == "/admin"
        )
        admin_paths = {r.path for r in admin_app.routes}
        assert "/refresh-schema" in admin_paths
        assert "/pool-sizing" in admin_paths
        assert "/reload-config" in admin_paths
        assert "/catalog" in admin_paths
        assert "/queries" in admin_paths

    def test_title_uses_deployment_name(self):
        # Default APP_NAME is "app" → header slug "App"; title appends
        # the deployment_name suffix.
        assert _build(settings=_settings("inv")).title == "App — inv"

    def test_extra_routes_callback_invoked(self):
        seen: list = []

        def hook(app: FastAPI, settings: Any) -> None:
            seen.append((app, settings))

            @app.get("/_extra")
            async def _extra():
                return {"hello": "extra"}

        settings = _settings()
        app = _build(settings=settings, extra_routes=hook)
        assert len(seen) == 1
        assert seen[0][0] is app
        assert seen[0][1] is settings
        assert "/_extra" in {r.path for r in app.routes}

    def test_extra_routes_optional(self):
        """No extra_routes — build_app must not crash."""
        app = _build()
        assert "/_extra" not in {r.path for r in app.routes}

    def test_health_payload_default(self):
        with TestClient(_build()) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        # Default APP_NAME="app" → no deployment_name → no header at all.
        assert "X-App-Deployment" not in resp.headers

    def test_health_payload_with_deployment_name(self):
        app = _build(settings=_settings("infra_pg_east"))
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.json() == {"status": "ok", "deployment": "infra_pg_east"}
        # Default APP_NAME="app" → header is X-App-Deployment.
        assert resp.headers.get("X-App-Deployment") == "infra_pg_east"

    def test_lifespan_inits_and_closes_pool(self):
        db = _fake_db_module()
        with TestClient(_build(db=db)) as client:
            client.get("/health")
        db.init_pool.assert_awaited_once()
        db.close_pool.assert_awaited_once()


class TestOpenAPISplit:
    """Admin/dev OpenAPI split + dynamic per-table spec. (#253)"""

    def test_dev_openapi_excludes_admin_endpoints(self):
        with TestClient(_build()) as client:
            spec = client.get("/openapi.json").json()
        paths = set(spec["paths"].keys())
        # Admin endpoints live on the mounted sub-app; they must not
        # leak into the dev spec consumers download.
        assert not any(p.startswith("/admin") for p in paths), (
            f"admin paths leaked into dev spec: {sorted(paths)!r}"
        )
        # The generic /{schema}/{view_name} placeholder is hidden so
        # devs see concrete tables instead.
        assert "/{schema}/{view_name}" not in paths
        # Dev-facing essentials still present.
        assert "/health" in paths
        assert "/ready" in paths

    def test_admin_openapi_houses_admin_endpoints(self):
        # Post-#302, /admin/openapi.json requires the admin token (the
        # auto-mounted endpoint no longer leaks the spec publicly).
        settings = _settings()
        settings.auth.admin_token = "test-token"
        with TestClient(_build(settings=settings)) as client:
            spec = client.get(
                "/admin/openapi.json", headers={"X-Admin-Token": "test-token"}
            ).json()
        paths = set(spec["paths"].keys())
        assert "/refresh-schema" in paths
        assert "/pool-sizing" in paths
        assert "/reload-config" in paths
        assert "/catalog" in paths
        assert "/queries" in paths

    def test_dev_docs_swagger_separate_from_admin_docs(self):
        # /admin/docs also requires the admin token post-#302.
        settings = _settings()
        settings.auth.admin_token = "test-token"
        with TestClient(_build(settings=settings)) as client:
            dev = client.get("/docs")
            admin = client.get(
                "/admin/docs", headers={"X-Admin-Token": "test-token"}
            )
        assert dev.status_code == 200
        assert admin.status_code == 200
        assert dev.text != admin.text  # different titles per FastAPI sub-app

    def test_dynamic_spec_lists_concrete_tables(self):
        """Default (simple-schema) mode: tables enumerated as concrete
        paths; simple-filter params collapsed into one generic entry per
        table. Column list lives in the operation description (not
        repeated per parameter)."""
        schema_cache._cache.clear()
        schema_cache._cache["public"] = {
            "products": {"id", "name", "category"},
            "orders": {"id", "user_id"},
        }

        async def harvest_with_seed():
            return dict(schema_cache._cache)

        db = _fake_db_module()
        db.harvest_ddl = harvest_with_seed

        with TestClient(_build(db=db)) as client:
            spec = client.get("/openapi.json").json()
        paths = set(spec["paths"].keys())

        assert "/public/products" in paths
        assert "/public/orders" in paths

        products_op = spec["paths"]["/public/products"]["get"]
        param_names = {p["name"] for p in products_op["parameters"]}
        # Closed-grammar pagination params present.
        assert "$select" in param_names
        assert "$filter" in param_names
        assert "$orderby" in param_names
        # simple-schema: one generic simple-filter entry.
        assert "<any-listed-column>" in param_names
        # No per-column entries in default mode.
        assert "id" not in param_names
        assert "name" not in param_names
        assert "category" not in param_names
        # Column list surfaced in the operation description (the
        # canonical place); per-param descriptions point to it rather
        # than enumerating, to keep wide-table specs readable.
        op_description = products_op["description"]
        assert "category" in op_description
        assert "id" in op_description
        assert "name" in op_description
        # Per-param descriptions reference the operation description
        # instead of repeating the column list.
        generic = next(p for p in products_op["parameters"] if p["name"] == "<any-listed-column>")
        assert "operation description" in generic["description"]

    def test_full_schema_mode_emits_per_column_simple_filter_params(self):
        """full-schema mode preserves the pre-#268 exhaustive shape —
        every column gets its own concrete simple-filter param."""
        schema_cache._cache.clear()
        schema_cache._cache["public"] = {"products": {"id", "name", "category"}}

        async def harvest_with_seed():
            return dict(schema_cache._cache)

        settings = _settings()
        settings.openapi_render_mode = "full-schema"

        db = _fake_db_module()
        db.harvest_ddl = harvest_with_seed

        with TestClient(_build(settings=settings, db=db)) as client:
            spec = client.get("/openapi.json").json()

        params = {
            p["name"]: p
            for p in spec["paths"]["/public/products"]["get"]["parameters"]
        }
        # Every column surfaces as a concrete simple-filter param.
        assert "id" in params
        assert "name" in params
        assert "category" in params
        # No generic placeholder when concrete params cover the surface.
        assert "<any-listed-column>" not in params

    def test_view_def_required_columns_not_marked_required_in_simple_filter(self):
        """View-def required params surface in the description of the
        simple-filter param (whether generic in simple-schema or
        concrete in full-schema). They are NOT marked
        ``required: true`` at the OpenAPI level — the runtime accepts
        either a simple ``?col=val`` filter OR an equality constraint
        inside ``$filter`` (#187), so client-side validation must not
        reject the latter form."""
        from core import openapi_dynamic
        from core.loaders import validation as view_defs
        from core.loaders.contract import ParamContract

        schema_cache._cache.clear()
        schema_cache._cache["public"] = {"products": {"id", "name", "category"}}

        async def harvest_with_seed():
            return dict(schema_cache._cache)

        # Use full-schema so the test directly verifies the per-column
        # shape that previously was the default. simple-schema mode's
        # required-param surfacing is checked in the generic-param
        # description elsewhere.
        settings = _settings()
        settings.openapi_render_mode = "full-schema"

        db = _fake_db_module()
        db.harvest_ddl = harvest_with_seed

        app = _build(settings=settings, db=db)
        try:
            with TestClient(app) as client:
                view_defs._defs["public"] = {
                    "products": ParamContract(required=["id"], optional=["category"]),
                }
                openapi_dynamic.invalidate(app)
                spec = client.get("/openapi.json").json()

            params = {
                p["name"]: p
                for p in spec["paths"]["/public/products"]["get"]["parameters"]
            }
            # Required-by-view-def column surfaces as a concrete param
            # but is NOT marked required — the OR semantic with $filter
            # lives in the description.
            assert params["id"]["required"] is False
            assert "Required by view def" in params["id"]["description"]
            assert "$filter" in params["id"]["description"]
        finally:
            view_defs._defs.clear()

    def test_dynamic_spec_lists_custom_queries(self):
        from core import openapi_dynamic
        from core.loaders import queries as custom_queries
        from core.loaders.contract import ParamContract
        from core.loaders.queries import QueryDef

        app = _build()
        try:
            with TestClient(app) as client:
                # Populate after lifespan (which calls load_queries and clears
                # the registry from disk). Invalidate the cached spec so the
                # next /openapi.json regenerates with our entry.
                custom_queries._queries["reports/sites_by_state"] = QueryDef(
                    sql="SELECT * FROM sites WHERE state = :state",
                    view_def=ParamContract(required=["state"], optional=["region"]),
                    name="sites_by_state",
                )
                openapi_dynamic.invalidate(app)
                spec = client.get("/openapi.json").json()
            paths = set(spec["paths"].keys())
            assert "/queries/reports/sites_by_state" in paths
            op = spec["paths"]["/queries/reports/sites_by_state"]["get"]
            params = {p["name"]: p for p in op["parameters"]}
            assert params["state"]["required"] is True
            assert params["region"]["required"] is False
        finally:
            custom_queries._queries.clear()

    def test_admin_catalog_payload_shape(self):
        schema_cache._cache.clear()
        schema_cache._cache["public"] = {
            "products": {"id", "name"},
        }

        async def harvest_with_seed():
            return dict(schema_cache._cache)

        db = _fake_db_module()
        db.harvest_ddl = harvest_with_seed

        # Catalog is admin-token gated; supply a settings with the token
        # set and pass it on the request.
        settings = _settings()
        settings.auth.admin_token = "test-token"

        with TestClient(_build(settings=settings, db=db)) as client:
            resp = client.get(
                "/admin/catalog", headers={"X-Admin-Token": "test-token"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "schemas" in body
        assert "queries" in body
        assert body["schemas"]["public"]["products"]["columns"] == ["id", "name"]
        assert body["schemas"]["public"]["products"]["view_def"] is None

    def test_admin_catalog_requires_admin_token(self):
        # No token configured → admin endpoints fail-closed.
        with TestClient(_build()) as client:
            resp = client.get("/admin/catalog")
        assert resp.status_code == 401

    def test_admin_subapp_dependency_gates_unprotected_routes(self):
        """Defense-in-depth (#260): the admin sub-app declares
        ``Depends(verify_admin_token)`` at construction. A future route
        added without an explicit per-handler check is still gated by
        the sub-app dependency. This test simulates that by adding a
        route AFTER build_app and confirming it 401s without a token.
        """
        app = _build()
        # Find the mounted admin sub-app and tack on a new route.
        admin_app = next(
            r.app for r in app.routes
            if getattr(r, "path", None) == "/admin"
            and hasattr(r, "app")
        )

        @admin_app.get("/unprotected-by-handler")
        async def _new_endpoint():
            # Intentionally missing a verify_admin_token() call. The
            # sub-app dependency is what protects this route.
            return {"ok": True}

        with TestClient(app) as client:
            resp = client.get("/admin/unprotected-by-handler")
        assert resp.status_code == 401, (
            "admin sub-app dependency failed to gate a handler that "
            "didn't call verify_admin_token() itself"
        )

    def test_admin_openapi_declares_admin_token_security(self):
        settings = _settings()
        settings.auth.admin_token = "test-token"
        with TestClient(_build(settings=settings)) as client:
            spec = client.get(
                "/admin/openapi.json", headers={"X-Admin-Token": "test-token"}
            ).json()
        schemes = spec["components"]["securitySchemes"]
        assert "AdminToken" in schemes
        assert schemes["AdminToken"]["in"] == "header"
        assert schemes["AdminToken"]["name"] == "X-Admin-Token"
        # Every admin operation should reference the scheme.
        for path, item in spec["paths"].items():
            for method, op in item.items():
                if not isinstance(op, dict) or "operationId" not in op:
                    continue
                assert op.get("security") == [{"AdminToken": []}], (
                    f"admin op {method.upper()} {path} missing AdminToken security"
                )

    def test_dev_openapi_declares_passthrough_security_always(self):
        with TestClient(_build()) as client:
            spec = client.get("/openapi.json").json()
        schemes = spec.get("components", {}).get("securitySchemes", {})
        # Standard http:basic on Authorization — Swagger UI gives a
        # username/password form and auto-encodes. The recommended path
        # for deployments without JWT.
        assert "PassthroughBasic" in schemes
        assert schemes["PassthroughBasic"]["type"] == "http"
        assert schemes["PassthroughBasic"]["scheme"] == "basic"
        # X-DB-Authorization form — for combined JWT + DB passthrough.
        assert "PassthroughXDB" in schemes
        assert schemes["PassthroughXDB"]["name"] == "X-DB-Authorization"
        # JWT scheme only appears when AUTH__JWKS_URL is configured.
        assert "JWT" not in schemes

    def test_dev_openapi_declares_jwt_security_when_configured(self):
        settings = _settings()
        settings.auth.jwks_url = "https://example/.well-known/jwks.json"
        with TestClient(_build(settings=settings)) as client:
            spec = client.get("/openapi.json").json()
        schemes = spec["components"]["securitySchemes"]
        assert "JWT" in schemes
        assert "PassthroughBasic" in schemes
        assert "PassthroughXDB" in schemes


class TestAllowlistPresentationMode:
    """#291 — presentation mode filters the OpenAPI spec without
    affecting the DDL cache. Tables not matching the allowlist are
    absent from the spec but still reachable via direct URL."""

    def test_presentation_mode_filters_spec(self):
        from core import openapi_dynamic
        from core.loaders import allowlist as _allowlist

        schema_cache._cache.clear()
        schema_cache._cache["public"] = {
            "products": {"id"},
            "fact_revenue": {"id"},
        }
        schema_cache._cache["audit"] = {"events": {"id"}}

        async def harvest_with_seed():
            return dict(schema_cache._cache)

        settings = _settings()
        settings.allowlist_mode = "presentation"

        db = _fake_db_module()
        db.harvest_ddl = harvest_with_seed

        try:
            with TestClient(_build(settings=settings, db=db)) as client:
                # Override the loaded allowlist AFTER lifespan startup
                # (which resets _loaded via allowlist.load()). The spec
                # is built lazily on first /openapi.json request, so the
                # override is in effect when build_dev_openapi runs.
                _allowlist._loaded = _allowlist.Allowlist(
                    schemas=["public", "audit"],
                    tables=["public.products", "audit.events"],
                )
                openapi_dynamic.invalidate(client.app)
                spec = client.get("/openapi.json").json()

            paths = set(spec["paths"].keys())
            assert "/public/products" in paths
            assert "/audit/events" in paths
            # fact_revenue is in the cache but not the allowlist → spec filters it.
            assert "/public/fact_revenue" not in paths

            # Cache stays unfiltered — the table is still reachable directly.
            assert "fact_revenue" in schema_cache._cache["public"]
        finally:
            _allowlist._loaded = _allowlist.Allowlist()

    def test_enforce_mode_drops_from_cache(self):
        """Same allowlist, different mode, different observable
        behavior: in enforce mode the cache itself is narrowed (not
        just the spec)."""
        from core import openapi_dynamic
        from core.loaders import allowlist as _allowlist

        schema_cache._cache.clear()
        schema_cache._cache["public"] = {"products": {"id"}, "fact_revenue": {"id"}}

        async def harvest_with_seed():
            return dict(schema_cache._cache)

        settings = _settings()
        settings.allowlist_mode = "enforce"

        db = _fake_db_module()
        db.harvest_ddl = harvest_with_seed

        try:
            with TestClient(_build(settings=settings, db=db)) as client:
                # Override allowlist after lifespan startup, then call
                # narrow_cache directly to simulate what would happen if
                # an allowlist.yaml had been on disk at startup.
                _allowlist._loaded = _allowlist.Allowlist(
                    tables=["public.products"]
                )
                _allowlist.narrow_cache(
                    schema_cache._cache, mode=settings.allowlist_mode
                )
                openapi_dynamic.invalidate(client.app)
                spec = client.get("/openapi.json").json()

            paths = set(spec["paths"].keys())
            assert "/public/products" in paths
            assert "/public/fact_revenue" not in paths
            # In enforce mode the cache itself was narrowed.
            assert "fact_revenue" not in schema_cache._cache.get("public", {})
        finally:
            _allowlist._loaded = _allowlist.Allowlist()


class TestRequirePassthroughEnforcement:
    """#261 — when ``auth.require_passthrough`` is true (production
    default), data routes 401 without passthrough creds. The fail-closed
    posture protects deployments running with a service account that has
    full data access (e.g., the demo seed) — without the flag, every
    unauthenticated caller silently inherits the service account's
    privileges."""

    def test_data_route_401_without_creds(self):
        schema_cache._cache.clear()
        schema_cache._cache["public"] = {"products": {"id", "name"}}

        async def harvest_with_seed():
            return dict(schema_cache._cache)

        settings = _settings()
        settings.auth.require_passthrough = True

        db = _fake_db_module()
        db.harvest_ddl = harvest_with_seed

        with TestClient(_build(settings=settings, db=db)) as client:
            resp = client.get("/public/products")

        # Body collapses to "Unauthorized" under the terse default; the
        # status code is the load-bearing assertion.
        assert resp.status_code == 401

    def test_data_route_passes_with_creds(self):
        import base64

        schema_cache._cache.clear()
        schema_cache._cache["public"] = {"products": {"id", "name"}}

        async def harvest_with_seed():
            return dict(schema_cache._cache)

        settings = _settings()
        settings.auth.require_passthrough = True

        db = _fake_db_module()
        db.harvest_ddl = harvest_with_seed

        creds = base64.b64encode(b"alice:secret").decode()
        with TestClient(_build(settings=settings, db=db)) as client:
            resp = client.get(
                "/public/products",
                headers={"X-DB-Authorization": f"Basic {creds}"},
            )

        # Fake DB module returns canned rows (creds aren't actually
        # validated against a DB). The relevant assertion is that the
        # 401 short-circuit didn't fire.
        assert resp.status_code == 200


class TestOpenAPIVisibility:
    """#302 — OPENAPI__VISIBILITY controls whether /docs, /redoc, and
    /openapi.json are reachable. Three modes:

    - ``enabled`` (default): all three reachable unauthenticated.
    - ``admin-enabled``: HTML pages 404, JSON requires admin token.
    - ``disabled``: all three 404.

    Admin sub-app (/admin/docs, /admin/openapi.json) is unaffected — already
    token-gated by the sub-app dependency."""

    def test_enabled_serves_all_three(self):
        with TestClient(_build()) as client:
            assert client.get("/openapi.json").status_code == 200
            assert client.get("/docs").status_code == 200
            assert client.get("/redoc").status_code == 200

    def test_admin_enabled_disables_html(self):
        settings = _settings()
        settings.openapi_visibility = "admin-enabled"
        with TestClient(_build(settings=settings)) as client:
            assert client.get("/docs").status_code == 404
            assert client.get("/redoc").status_code == 404

    def test_admin_enabled_json_requires_admin_token(self):
        settings = _settings()
        settings.openapi_visibility = "admin-enabled"
        settings.auth.admin_token = "test-token"
        with TestClient(_build(settings=settings)) as client:
            # No token → 401.
            assert client.get("/openapi.json").status_code == 401
            # Bad token → 401.
            assert (
                client.get(
                    "/openapi.json", headers={"X-Admin-Token": "wrong"}
                ).status_code
                == 401
            )
            # Correct token → 200 with the same spec the standard endpoint
            # would have served.
            resp = client.get(
                "/openapi.json", headers={"X-Admin-Token": "test-token"}
            )
            assert resp.status_code == 200
            spec = resp.json()
            assert "paths" in spec
            assert "info" in spec

    def test_disabled_404s_everything(self):
        settings = _settings()
        settings.openapi_visibility = "disabled"
        with TestClient(_build(settings=settings)) as client:
            assert client.get("/openapi.json").status_code == 404
            assert client.get("/docs").status_code == 404
            assert client.get("/redoc").status_code == 404

    def test_admin_sub_app_unaffected_by_visibility(self):
        """The admin sub-app's /admin/docs and /admin/openapi.json
        respect the existing admin-token gate regardless of the
        user-facing visibility mode."""
        settings = _settings()
        settings.openapi_visibility = "disabled"
        settings.auth.admin_token = "test-token"
        with TestClient(_build(settings=settings)) as client:
            # Without token, admin endpoints 401.
            assert client.get("/admin/openapi.json").status_code == 401
            # With token, admin endpoints work.
            resp = client.get(
                "/admin/openapi.json", headers={"X-Admin-Token": "test-token"}
            )
            assert resp.status_code == 200

    def test_admin_spec_endpoints_require_token_in_default_mode(self):
        """Independent of OPENAPI__VISIBILITY, the admin sub-app's spec
        and docs endpoints require X-Admin-Token. Pre-#302 the auto-
        mounted endpoints bypassed the sub-app dependency and were
        publicly accessible — this test pins the post-fix behavior so
        a regression here surfaces immediately."""
        settings = _settings()
        settings.auth.admin_token = "test-token"
        # Default openapi_visibility = "enabled" — user-facing surface
        # open, admin surface still gated.
        with TestClient(_build(settings=settings)) as client:
            for path in ("/admin/openapi.json", "/admin/docs", "/admin/redoc"):
                # No token → 401.
                assert client.get(path).status_code == 401, (
                    f"{path} should require admin token but didn't"
                )
                # With token → 200.
                resp = client.get(path, headers={"X-Admin-Token": "test-token"})
                assert resp.status_code == 200, (
                    f"{path} should serve content with valid token, got {resp.status_code}"
                )
