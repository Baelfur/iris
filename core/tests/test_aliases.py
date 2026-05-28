"""Tests for core.aliases — route alias registration + conflict detection. (#270)"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core import aliases as alias_routes
from core import context as _context
from core import openapi_dynamic
from core.app_meta import build_app
from core.config.settings import AuthSettings, ConfigSettings, AppSettings
from core.engine import schema_cache
from core.loaders import queries as custom_queries
from core.loaders import validation as view_defs
from core.loaders.contract import ParamContract
from core.loaders.queries import QueryDef


def _settings() -> AppSettings:
    # require_passthrough=False because these end-to-end tests hit data
    # routes without supplying creds (fake DB module returns canned rows).
    # Tests that want to verify the require-passthrough enforcement set
    # the flag explicitly.
    return AppSettings(
        auth=AuthSettings(mode="gateway", require_passthrough=False),
        config=ConfigSettings(source="local"),
    )


def _fake_db():
    class _FakeDB:
        init_pool = AsyncMock()
        close_pool = AsyncMock()

        @staticmethod
        async def harvest_ddl():
            return {"public": {"products": {"id", "name"}}}

        @staticmethod
        async def fetch_all(sql, params=None):
            return [{"id": 1, "name": "Laptop"}]

        @staticmethod
        async def fetch_all_with_creds(sql, params, creds):
            return [{"id": 1, "name": "Laptop"}]

        @staticmethod
        async def ping():
            return None

        @staticmethod
        async def get_connection_limit():
            return (None, "fake")

    return _FakeDB()


@pytest.fixture(autouse=True)
def _reset_state():
    _context._ctx = None
    schema_cache._cache.clear()
    view_defs._defs.clear()
    view_defs._aliases.clear()
    custom_queries._queries.clear()
    alias_routes._accepted_by_target.clear()
    yield
    _context._ctx = None
    schema_cache._cache.clear()
    view_defs._defs.clear()
    view_defs._aliases.clear()
    custom_queries._queries.clear()
    alias_routes._accepted_by_target.clear()


class TestReservedPrefixes:
    """Aliases that collide with paths the service owns are rejected at load time."""

    @pytest.mark.parametrize("alias", [
        "/health",
        "/ready",
        "/readyz",
        "/admin",
        "/admin/anything",
        "/admin/queries/inner",
        "/queries",
        "/queries/something",
        "/openapi.json",
        "/docs",
        "/redoc",
    ])
    def test_reserved_prefix_rejected(self, alias):
        assert alias_routes._is_reserved(alias) is not None

    @pytest.mark.parametrize("alias", [
        "/legacy/crm/customers",
        "/legacy/inventory/products",
        "/v1/customers/by-region",
        "/foo/bar",
    ])
    def test_normal_alias_accepted(self, alias):
        assert alias_routes._is_reserved(alias) is None

    def test_must_start_with_slash(self):
        assert alias_routes._is_reserved("relative/path") is not None


class TestAliasCollection:
    """`_collect_aliases` walks both YAML registries and partitions
    aliases into accepted / rejected piles."""

    def test_view_def_alias_accepted(self):
        view_defs._defs["public"] = {
            "products": ParamContract(required=[], optional=[]),
        }
        view_defs._aliases[("public", "products")] = ["/legacy/products"]

        accepted, rejected = alias_routes._collect_aliases()
        assert len(accepted) == 1
        origin, target, alias = accepted[0]
        assert origin == "view_def:public.products"
        assert target.path == "/public/products"
        assert alias == "/legacy/products"
        assert rejected == []

    def test_query_alias_accepted(self):
        custom_queries._queries["reports/by_cat"] = QueryDef(
            sql="SELECT 1", view_def=ParamContract([], []),
            name="by_cat", aliases=["/legacy/reports/by-category"],
        )
        accepted, rejected = alias_routes._collect_aliases()
        assert len(accepted) == 1
        origin, target, alias = accepted[0]
        assert origin == "query:reports/by_cat"
        assert target.path == "/queries/reports/by_cat"
        assert alias == "/legacy/reports/by-category"
        assert rejected == []

    def test_collision_between_aliases_rejects_second(self):
        view_defs._defs["public"] = {
            "a": ParamContract(required=[], optional=[]),
            "b": ParamContract(required=[], optional=[]),
        }
        view_defs._aliases[("public", "a")] = ["/legacy/shared"]
        view_defs._aliases[("public", "b")] = ["/legacy/shared"]

        accepted, rejected = alias_routes._collect_aliases()
        accepted_aliases = [a for _, _, a in accepted]
        # First wins; second is rejected with a clear reason.
        assert accepted_aliases.count("/legacy/shared") == 1
        assert any("/legacy/shared" in r[2] for r in rejected)

    def test_reserved_alias_rejected(self):
        view_defs._defs["public"] = {
            "products": ParamContract(required=[], optional=[]),
        }
        view_defs._aliases[("public", "products")] = ["/admin/sneaky"]

        accepted, _ = alias_routes._collect_aliases()
        assert all(a != "/admin/sneaky" for _, _, a in accepted)


class TestShadowDetection:
    """An alias matching `/<schema>/<table>` shadows a real dynamic route."""

    def test_shadow_detected(self):
        schema_cache._cache["public"] = {"products": {"id"}}
        result = alias_routes._shadows_dynamic_route("/public/products")
        assert result == ("public", "products")

    def test_no_shadow_when_table_absent(self):
        schema_cache._cache["public"] = {"products": {"id"}}
        result = alias_routes._shadows_dynamic_route("/public/orders")
        assert result is None

    def test_three_segment_alias_does_not_shadow(self):
        schema_cache._cache["public"] = {"products": {"id"}}
        # /legacy/products is 3 segments — doesn't match the
        # 2-segment dynamic route shape.
        assert alias_routes._shadows_dynamic_route("/legacy/products") is None


class TestEndToEnd:
    """Full lifespan: YAMLs declare aliases, lifespan registers routes,
    requests at the alias paths reach the canonical handler."""

    def test_table_alias_routes_to_canonical_handler(self):
        # Pre-seed BEFORE building the app so the lifespan picks them up.
        # The lifespan's load_views() WOULD reset _defs, but the test's
        # FakeDB's harvest plus our manual seed is the simplest path:
        # we use the post-lifespan hook by registering aliases ourselves.
        app = build_app(
            database="postgresql", paramstyle="pyformat",
            settings=_settings(), db_module=_fake_db(), db_user="app",
        )
        with TestClient(app) as client:
            # Inject after lifespan has run: seed loader state and call
            # register_all directly (mirrors what lifespan does).
            view_defs._defs["public"] = {
                "products": ParamContract(required=[], optional=[]),
            }
            view_defs._aliases[("public", "products")] = ["/legacy/products"]
            alias_routes.register_all(app)

            # Canonical and alias paths return identical results.
            canonical = client.get("/public/products")
            aliased = client.get("/legacy/products")
            assert canonical.status_code == 200
            assert aliased.status_code == 200
            assert canonical.json() == aliased.json()

    def test_query_alias_routes_to_canonical_handler(self):
        app = build_app(
            database="postgresql", paramstyle="pyformat",
            settings=_settings(), db_module=_fake_db(), db_user="app",
        )
        with TestClient(app) as client:
            custom_queries._queries["reports/by_cat"] = QueryDef(
                sql="SELECT 1", view_def=ParamContract([], []),
                name="by_cat", aliases=["/legacy/by-category"],
            )
            alias_routes.register_all(app)
            openapi_dynamic.invalidate(app)

            canonical = client.get("/queries/reports/by_cat")
            aliased = client.get("/legacy/by-category")
            assert canonical.status_code == 200
            assert aliased.status_code == 200
            assert canonical.json() == aliased.json()


class TestOpenAPIIntegration:
    """Aliases appear in the canonical operation's description, not as
    separate operation entries (option B; spec stays lean)."""

    def test_alias_appears_in_table_operation_description(self):
        app = build_app(
            database="postgresql", paramstyle="pyformat",
            settings=_settings(), db_module=_fake_db(), db_user="app",
        )
        with TestClient(app) as client:
            view_defs._defs["public"] = {
                "products": ParamContract(required=[], optional=[]),
            }
            view_defs._aliases[("public", "products")] = [
                "/legacy/products", "/legacy/products",
            ]
            alias_routes.register_all(app)
            openapi_dynamic.invalidate(app)
            spec = client.get("/openapi.json").json()

        op = spec["paths"]["/public/products"]["get"]
        assert "/legacy/products" in op["description"]
        assert "/legacy/products" in op["description"]
        # Aliases are NOT separate operation entries — option B.
        assert "/legacy/products" not in spec["paths"]
        assert "/legacy/products" not in spec["paths"]

    def test_alias_appears_in_query_operation_description(self):
        app = build_app(
            database="postgresql", paramstyle="pyformat",
            settings=_settings(), db_module=_fake_db(), db_user="app",
        )
        with TestClient(app) as client:
            custom_queries._queries["reports/by_cat"] = QueryDef(
                sql="SELECT 1", view_def=ParamContract([], []),
                name="by_cat", aliases=["/legacy/by-category"],
            )
            alias_routes.register_all(app)
            openapi_dynamic.invalidate(app)
            spec = client.get("/openapi.json").json()

        op = spec["paths"]["/queries/reports/by_cat"]["get"]
        assert "/legacy/by-category" in op["description"]

    def test_rejected_alias_does_not_appear_in_spec(self):
        """Aliases rejected at registration (reserved-prefix collision,
        inter-alias collision) must NOT show up in the OpenAPI
        description. The spec describes only what's actually
        reachable."""
        app = build_app(
            database="postgresql", paramstyle="pyformat",
            settings=_settings(), db_module=_fake_db(), db_user="app",
        )
        with TestClient(app) as client:
            view_defs._defs["public"] = {
                "products": ParamContract(required=[], optional=[]),
            }
            # First one accepted; second collides with a reserved
            # prefix and is rejected.
            view_defs._aliases[("public", "products")] = [
                "/legacy/products",
                "/admin/sneaky",
            ]
            alias_routes.register_all(app)
            openapi_dynamic.invalidate(app)
            spec = client.get("/openapi.json").json()

        op = spec["paths"]["/public/products"]["get"]
        assert "/legacy/products" in op["description"]
        assert "/admin/sneaky" not in op["description"]
