"""Tests for core.query_engine — QueryParams, build_links, extract_simple_filters.

Paramstyle-specific SQL emission lives in each variant's test suite since it's
the part that differs. Everything here is paramstyle-independent.
"""

import pytest

from core.engine import schema_cache
from core.engine.query_engine import QueryParams, build_links, build_query, extract_simple_filters


class TestBuildLinks:
    def test_no_links_without_count(self):
        assert build_links(QueryParams(), 10) == []

    def test_no_links_when_fewer_rows(self):
        assert build_links(QueryParams(count=10), 5) == []

    def test_next_link_when_full_page(self):
        links = build_links(QueryParams(count=5), 5)
        assert len(links) == 1
        assert links[0]["rel"] == "next"
        assert "start_index=5" in links[0]["href"]
        assert "count=5" in links[0]["href"]

    def test_next_link_with_offset(self):
        links = build_links(QueryParams(count=5, start_index=10), 5)
        assert "start_index=15" in links[0]["href"]

    def test_preserves_params_in_link(self):
        params = QueryParams(select="id", filter_="id eq 1", orderby="id", count=2)
        links = build_links(params, 2)
        href = links[0]["href"]
        assert "select" in href
        assert "filter" in href
        assert "orderby" in href

    def test_preserves_groupby_and_having_in_link(self):
        params = QueryParams(
            select="category", groupby="category",
            having="category eq 'books'", count=2,
        )
        href = build_links(params, 2)[0]["href"]
        assert "groupby" in href
        assert "having" in href

    def test_preserves_simple_filters_in_link(self):
        """Simple ?col=val filters must survive into the next-page URL —
        otherwise pagination silently broadens the result set."""
        params = QueryParams(
            count=5, simple_filters={"category": "electronics", "status": "open"},
        )
        href = build_links(params, 5)[0]["href"]
        assert "category=electronics" in href
        assert "status=open" in href

    def test_cursor_replaces_start_index_in_link(self):
        """With cursor pagination, the next link advances by `$cursor=<token>`
        rather than `$start_index=N` — the cursor is the page marker."""
        params = QueryParams(count=5, orderby="id ASC")
        links = build_links(params, 5, cursor="ABC.XYZ")
        href = links[0]["href"]
        assert "cursor=ABC" in href
        assert "start_index" not in href


@pytest.fixture
def seed_cache():
    schema_cache._cache.clear()
    schema_cache._cache["public"] = {"products": {"id", "name", "category"}}
    yield
    schema_cache._cache.clear()


class TestKeysetEmission:
    """Cursor pagination emits a portable WHERE shape: per-column OR
    chain of equality + comparison so every supported DB executes it
    without needing row-constructor support. (#244)"""

    def test_single_column_asc(self, seed_cache):
        params = QueryParams(
            select="id",
            orderby="id ASC",
            count=10,
            cursor_keyset=([("id", "ASC")], [42]),
        )
        sql, binds = build_query("public", "products", params, paramstyle="pyformat")
        assert "WHERE" in sql
        assert "id > %(c_cmp_0)s" in sql
        assert binds["c_cmp_0"] == 42
        # No OFFSET when cursor pagination is in play (start_index is None).
        assert "OFFSET" not in sql
        assert "LIMIT 10" in sql

    def test_single_column_desc_uses_lt(self, seed_cache):
        params = QueryParams(
            orderby="id DESC",
            count=10,
            cursor_keyset=([("id", "DESC")], [42]),
        )
        sql, _ = build_query("public", "products", params, paramstyle="pyformat")
        assert "id < %(c_cmp_0)s" in sql

    def test_multi_column_mixed_directions(self, seed_cache):
        # ORDER BY category ASC, id DESC with last seen ("books", 100):
        #   (category > "books") OR (category = "books" AND id < 100)
        params = QueryParams(
            orderby="category ASC, id DESC",
            count=10,
            cursor_keyset=([("category", "ASC"), ("id", "DESC")], ["books", 100]),
        )
        sql, binds = build_query("public", "products", params, paramstyle="pyformat")
        # First OR term: category strictly greater
        assert "(category > %(c_cmp_0)s)" in sql
        # Second OR term: category equal AND id strictly less
        assert "(category = %(c_eq_0)s AND id < %(c_cmp_1)s)" in sql
        # Top-level wrap so this WHERE composes with other predicates via AND
        assert sql.count(" OR ") >= 1
        assert binds["c_cmp_0"] == "books"
        assert binds["c_eq_0"] == "books"
        assert binds["c_cmp_1"] == 100

    def test_keyset_composes_with_filter(self, seed_cache):
        """User filters survive — the keyset clause AND's with $filter."""
        params = QueryParams(
            filter_="category eq 'widgets'",
            orderby="id ASC",
            count=10,
            cursor_keyset=([("id", "ASC")], [42]),
        )
        sql, _ = build_query("public", "products", params, paramstyle="pyformat")
        # $filter and the keyset clause both present, joined by AND
        assert "category = " in sql
        assert "id > " in sql
        assert " AND " in sql


class TestExtractSimpleFilters:
    def test_extracts_non_reserved(self):
        params = {
            "$select": "id", "$filter": "x eq 1",
            "name": "Laptop", "category": "electronics",
        }
        assert extract_simple_filters(params) == {
            "name": "Laptop", "category": "electronics",
        }

    def test_ignores_dollar_prefixed(self):
        params = {"$count": "5", "$orderby": "id", "id": "1"}
        assert extract_simple_filters(params) == {"id": "1"}

    def test_groupby_and_having_are_reserved(self):
        params = {"$groupby": "category", "$having": "x eq 1", "id": "1"}
        assert extract_simple_filters(params) == {"id": "1"}

    def test_empty_when_all_reserved(self):
        params = {"$select": "id", "$filter": "x eq 1"}
        assert extract_simple_filters(params) == {}
