"""Tests for core.catalog — shared catalog enumeration. (#256)

The two consumers (``/admin/catalog`` and ``openapi_dynamic``) get
their integration coverage in test_app_meta.py; these tests pin the
iterator contract directly so a future regression to the
yield-shape surfaces here rather than three layers up the stack.
"""

from __future__ import annotations

import pytest

from core import catalog
from core.engine import schema_cache
from core.loaders import queries as custom_queries
from core.loaders import validation as view_defs
from core.loaders.contract import ParamContract
from core.loaders.queries import QueryDef


@pytest.fixture(autouse=True)
def _reset_state():
    schema_cache._cache.clear()
    schema_cache._indexes.clear()
    view_defs._defs.clear()
    custom_queries._queries.clear()
    yield
    schema_cache._cache.clear()
    schema_cache._indexes.clear()
    view_defs._defs.clear()
    custom_queries._queries.clear()


class TestIterTables:
    def test_yields_per_table_entries(self):
        schema_cache._cache["public"] = {
            "products": {"id", "name"},
            "orders": {"id", "user_id"},
        }
        schema_cache._cache["audit"] = {"events": {"id", "ts"}}

        entries = list(catalog.iter_tables())
        assert len(entries) == 3
        keys = {(e.schema, e.table) for e in entries}
        assert keys == {("public", "products"), ("public", "orders"), ("audit", "events")}

    def test_columns_carried_through(self):
        schema_cache._cache["public"] = {"products": {"id", "name", "category"}}
        entry = next(catalog.iter_tables())
        assert entry.columns == {"id", "name", "category"}

    def test_view_def_none_when_no_yaml(self):
        schema_cache._cache["public"] = {"products": {"id"}}
        entry = next(catalog.iter_tables())
        assert entry.view_def is None

    def test_view_def_attached_when_yaml_present(self):
        schema_cache._cache["public"] = {"products": {"id", "name"}}
        view_defs._defs["public"] = {
            "products": ParamContract(required=["id"], optional=["name"]),
        }
        entry = next(catalog.iter_tables())
        assert entry.view_def is not None
        assert entry.view_def.required == {"id"}
        assert entry.view_def.optional == {"name"}

    def test_indexed_columns_attached(self):
        schema_cache._cache["public"] = {"products": {"id", "name"}}
        schema_cache._indexes["public"] = {"products": {"id"}}
        entry = next(catalog.iter_tables())
        assert entry.indexed_columns == {"id"}

    def test_indexed_columns_empty_when_unset(self):
        schema_cache._cache["public"] = {"products": {"id"}}
        entry = next(catalog.iter_tables())
        assert entry.indexed_columns == set()


class TestIterQueries:
    def test_yields_per_query_entries(self):
        custom_queries._queries["reports/by_cat"] = QueryDef(
            sql="SELECT 1",
            view_def=ParamContract(required=[], optional=[]),
            name="by_cat",
        )
        custom_queries._queries["reports/totals"] = QueryDef(
            sql="SELECT 2",
            view_def=ParamContract(required=[], optional=[]),
            name="totals",
        )
        entries = list(catalog.iter_queries())
        assert len(entries) == 2
        paths = {e.path for e in entries}
        assert paths == {"reports/by_cat", "reports/totals"}

    def test_qdef_carried_through(self):
        custom_queries._queries["x"] = QueryDef(
            sql="SELECT * FROM products WHERE id = :id",
            view_def=ParamContract(required=["id"], optional=[]),
            name="x_query",
        )
        entry = next(catalog.iter_queries())
        assert entry.path == "x"
        assert entry.qdef.name == "x_query"
        assert entry.qdef.view_def.required == {"id"}

    def test_no_queries_yields_nothing(self):
        assert list(catalog.iter_queries()) == []
