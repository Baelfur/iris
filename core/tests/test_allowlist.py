"""Tests for core.loaders.allowlist — schemas + tables narrowing
with glob support. (#269)"""

from __future__ import annotations

import pytest

from core.loaders import allowlist


@pytest.fixture(autouse=True)
def _reset():
    allowlist._loaded = allowlist.Allowlist()
    yield
    allowlist._loaded = allowlist.Allowlist()


class TestAllowlistMembership:
    def test_empty_allows_everything(self):
        a = allowlist.Allowlist()
        assert a.is_empty()
        assert a.schema_allowed("public")
        assert a.table_allowed("public", "products")

    def test_explicit_schemas(self):
        a = allowlist.Allowlist(schemas=["public", "audit"])
        assert a.schema_allowed("public")
        assert a.schema_allowed("PUBLIC")  # case-insensitive
        assert a.schema_allowed("audit")
        assert not a.schema_allowed("hr")

    def test_schema_glob(self):
        a = allowlist.Allowlist(schemas=["app_*"])
        assert a.schema_allowed("app_main")
        assert a.schema_allowed("app_reporting")
        assert not a.schema_allowed("public")

    def test_table_qualified_glob(self):
        a = allowlist.Allowlist(tables=["public.fact_*", "public.dim_customer"])
        assert a.table_allowed("public", "fact_orders")
        assert a.table_allowed("public", "fact_inventory")
        assert a.table_allowed("public", "dim_customer")
        assert not a.table_allowed("public", "products")
        assert not a.table_allowed("audit", "fact_orders")

    def test_table_section_only_no_schemas(self):
        # tables list narrows; empty schemas section means schemas
        # check is permissive.
        a = allowlist.Allowlist(tables=["public.products"])
        assert a.schema_allowed("public")
        assert a.schema_allowed("hr")
        assert a.table_allowed("public", "products")
        assert not a.table_allowed("public", "orders")
        assert not a.table_allowed("hr", "employees")

    def test_schemas_section_only_no_tables(self):
        a = allowlist.Allowlist(schemas=["public"])
        assert a.schema_allowed("public")
        assert not a.schema_allowed("hr")
        assert a.table_allowed("public", "anything")
        assert a.table_allowed("hr", "anything")  # tables filter permissive


class TestNarrowCache:
    def _seed_cache(self):
        return {
            "public": {
                "products": {"id", "name"},
                "orders": {"id", "user_id"},
                "fact_revenue": {"id", "amount"},
            },
            "audit": {
                "events": {"id", "ts"},
            },
            "hr": {
                "employees": {"id", "name"},
            },
        }

    def test_empty_allowlist_no_op(self):
        cache = self._seed_cache()
        allowlist._loaded = allowlist.Allowlist()
        count = allowlist.narrow_cache(cache)
        assert count == 5  # 3+1+1
        assert "hr" in cache
        assert "audit" in cache

    def test_schema_narrowing_drops_excluded_schemas(self):
        cache = self._seed_cache()
        allowlist._loaded = allowlist.Allowlist(schemas=["public", "audit"])
        count = allowlist.narrow_cache(cache)
        assert "hr" not in cache
        assert "public" in cache
        assert "audit" in cache
        assert count == 4  # 3 in public + 1 in audit

    def test_table_narrowing_drops_excluded_tables(self):
        cache = self._seed_cache()
        allowlist._loaded = allowlist.Allowlist(
            tables=["public.products", "public.fact_*"],
        )
        count = allowlist.narrow_cache(cache)
        assert "products" in cache["public"]
        assert "fact_revenue" in cache["public"]
        assert "orders" not in cache["public"]
        # Other schemas drop entirely (no matching tables).
        assert "audit" not in cache
        assert "hr" not in cache
        assert count == 2

    def test_schema_and_table_combined(self):
        cache = self._seed_cache()
        allowlist._loaded = allowlist.Allowlist(
            schemas=["public", "audit"],
            tables=["public.fact_*", "audit.events"],
        )
        count = allowlist.narrow_cache(cache)
        assert "public" in cache
        assert "audit" in cache
        assert "hr" not in cache
        assert "fact_revenue" in cache["public"]
        assert "products" not in cache["public"]
        assert "events" in cache["audit"]
        assert count == 2

    def test_drops_empty_schemas_after_table_narrowing(self):
        """A schema whose tables are all filtered out drops from the
        cache entirely — saves operators from seeing empty schemas
        in the dynamic surface listing."""
        cache = self._seed_cache()
        allowlist._loaded = allowlist.Allowlist(tables=["public.products"])
        allowlist.narrow_cache(cache)
        assert "audit" not in cache
        assert "hr" not in cache

    def test_presentation_mode_leaves_cache_untouched(self):
        """ALLOWLIST__MODE=presentation makes ``narrow_cache`` a no-op
        for the cache — the full DDL surface stays reachable. The
        OpenAPI renderer applies the filter at render time instead.
        (#291)"""
        cache = self._seed_cache()
        allowlist._loaded = allowlist.Allowlist(schemas=["public"])
        count = allowlist.narrow_cache(cache, mode="presentation")
        # Cache unchanged: every schema and every table still present.
        assert "public" in cache
        assert "audit" in cache
        assert "hr" in cache
        assert count == 5

    def test_enforce_mode_explicit_matches_default(self):
        cache_default = self._seed_cache()
        cache_explicit = self._seed_cache()
        allowlist._loaded = allowlist.Allowlist(schemas=["public"])
        allowlist.narrow_cache(cache_default)
        allowlist.narrow_cache(cache_explicit, mode="enforce")
        assert cache_default == cache_explicit


class TestLoad:
    def test_missing_file_returns_empty(self, tmp_path):
        a = allowlist.load(str(tmp_path))
        assert a.is_empty()

    def test_loads_yaml_file(self, tmp_path):
        (tmp_path / "allowlist.yaml").write_text(
            "schemas:\n  - public\ntables:\n  - public.fact_*\n"
        )
        a = allowlist.load(str(tmp_path))
        assert not a.is_empty()
        assert a.schema_allowed("public")
        assert a.table_allowed("public", "fact_orders")

    def test_invalid_yaml_treated_as_empty(self, tmp_path):
        (tmp_path / "allowlist.yaml").write_text(
            "schemas: not-a-list\n"
        )
        a = allowlist.load(str(tmp_path))
        assert a.is_empty()
