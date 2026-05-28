"""Variant-specific unit tests for Trino.

Paramstyle-specific SQL emission (qmark) is covered once for all
paramstyles in ``core/tests/test_paramstyle_emission.py``. This file
holds Trino-only behavior — the 3-segment qualifier emission for the
``/{catalog}/{schema}/{view}`` route (#152). (#200)
"""

from functools import partial

from core.engine.query_engine import QueryParams, build_query as _build_query

# Trino uses qmark paramstyle — binds are a positional list.
build_query = partial(_build_query, paramstyle="qmark")


def _setup_cache():
    import core.engine.schema_cache as sc

    sc._cache.clear()
    sc._cache["public"] = {
        "products": {"id", "name", "category", "price", "status"},
        "t": {"id", "name", "category", "price", "status", "city", "x"},
    }


def _clear_cache():
    import core.engine.schema_cache as sc

    sc._cache.clear()


class TestBuildQueryWithCatalog:
    """3-segment qualifier emission for the Trino /{catalog}/{schema}/{view} route. (#152)"""

    def setup_method(self):
        _setup_cache()

    def teardown_method(self):
        _clear_cache()

    def test_catalog_prefixes_qualified_name(self):
        sql, _ = build_query("public", "products", QueryParams(), catalog="hive")
        assert sql == "SELECT * FROM hive.public.products"

    def test_catalog_lowercased(self):
        sql, _ = build_query("public", "products", QueryParams(), catalog="HIVE")
        assert sql == "SELECT * FROM hive.public.products"

    def test_catalog_with_filter_and_paging(self):
        sql, binds = build_query(
            "public",
            "t",
            QueryParams(filter_="id eq 1", count=5, start_index=10),
            catalog="hive",
        )
        assert sql == "SELECT * FROM hive.public.t WHERE id = ? OFFSET 10 LIMIT 5"
        assert binds == [1]

    def test_catalog_none_emits_two_segment(self):
        sql, _ = build_query("public", "products", QueryParams(), catalog=None)
        assert sql == "SELECT * FROM public.products"
