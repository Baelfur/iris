"""build_query() output assertions parametrized over paramstyles.

This file replaces what each variant's ``test_unit.py`` was previously
asserting independently — the paramstyle-specific SQL shape from
``build_query()``. Paramstyle differences:

| paramstyle | placeholder | binds shape | table case | pagination          |
|------------|-------------|-------------|------------|---------------------|
| pyformat   | %(name)s    | dict        | lower      | LIMIT N OFFSET M    |
| named      | :name       | dict        | UPPER      | OFFSET M ROWS FETCH NEXT N ROWS ONLY |
| qmark      | ?           | list        | lower      | OFFSET M LIMIT N    |

Variant-specific behavior (Trino's 3-segment catalog route, postgres'
harvest_ddl SQL emission) stays in the variant's own ``test_unit.py``.
(#200)
"""

import pytest

from core.engine.expression import ExpressionError
from core.engine.query_engine import QueryParams, build_query


@pytest.fixture(autouse=True)
def _schema_cache():
    """Set up the DDL cache for the duration of one test, then clear it.

    All assertions here use the same fake schema/tables; pulling the
    setup into an autouse fixture removes the boilerplate setup_method/
    teardown_method that each variant test_unit.py was repeating.
    """
    import core.engine.schema_cache as sc
    sc._cache.clear()
    sc._cache["public"] = {
        "products": {"id", "name", "category", "price", "status"},
        "t": {"id", "name", "category", "price", "status", "city", "x"},
    }
    yield
    sc._cache.clear()


# --- Per-paramstyle expectations -------------------------------------------


@pytest.mark.parametrize("paramstyle,expected_sql,expected_binds", [
    ("pyformat", "SELECT * FROM public.products", {}),
    ("named",    "SELECT * FROM PUBLIC.PRODUCTS", {}),
    ("qmark",    "SELECT * FROM public.products", []),
])
def test_select_star(paramstyle, expected_sql, expected_binds):
    sql, binds = build_query("public", "products", QueryParams(), paramstyle=paramstyle)
    assert sql == expected_sql
    assert binds == expected_binds


@pytest.mark.parametrize("paramstyle,expected_sql", [
    ("pyformat", "SELECT id,name FROM public.t"),
    ("named",    "SELECT id,name FROM PUBLIC.T"),
    ("qmark",    "SELECT id,name FROM public.t"),
])
def test_select_columns(paramstyle, expected_sql):
    sql, _ = build_query("public", "t", QueryParams(select="id,name"), paramstyle=paramstyle)
    assert sql == expected_sql


@pytest.mark.parametrize("paramstyle,expected_sql,expected_binds", [
    ("pyformat", "SELECT * FROM public.t WHERE id = %(f0)s", {"f0": 1}),
    ("named",    "SELECT * FROM PUBLIC.T WHERE id = :f0",    {"f0": 1}),
    ("qmark",    "SELECT * FROM public.t WHERE id = ?",      [1]),
])
def test_filter_eq(paramstyle, expected_sql, expected_binds):
    sql, binds = build_query("public", "t", QueryParams(filter_="id eq 1"), paramstyle=paramstyle)
    assert sql == expected_sql
    assert binds == expected_binds


@pytest.mark.parametrize("paramstyle,placeholder_pattern,expected_binds", [
    ("pyformat", "city IN (%(f0)s, %(f1)s)", {"f0": "A", "f1": "B"}),
    ("named",    "city IN (:f0, :f1)",       {"f0": "A", "f1": "B"}),
    ("qmark",    "city IN (?, ?)",           ["A", "B"]),
])
def test_filter_in_list(paramstyle, placeholder_pattern, expected_binds):
    sql, binds = build_query(
        "public", "t",
        QueryParams(filter_="city in ('A', 'B')"),
        paramstyle=paramstyle,
    )
    assert placeholder_pattern in sql
    assert binds == expected_binds


@pytest.mark.parametrize("paramstyle", ["pyformat", "named", "qmark"])
def test_filter_null_becomes_is_null(paramstyle):
    """``status eq null`` lowers to ``status IS NULL`` regardless of paramstyle."""
    sql, _ = build_query(
        "public", "t",
        QueryParams(filter_="status eq null"),
        paramstyle=paramstyle,
    )
    assert "status IS NULL" in sql


@pytest.mark.parametrize("paramstyle,expected_sql", [
    ("pyformat", "SELECT * FROM public.t ORDER BY id DESC"),
    ("named",    "SELECT * FROM PUBLIC.T ORDER BY id DESC"),
    ("qmark",    "SELECT * FROM public.t ORDER BY id DESC"),
])
def test_orderby(paramstyle, expected_sql):
    sql, _ = build_query("public", "t", QueryParams(orderby="id DESC"), paramstyle=paramstyle)
    assert sql == expected_sql


# --- Pagination: each paramstyle has its own SQL syntax ---------------------


@pytest.mark.parametrize("paramstyle,expected_substrings,order_check", [
    ("pyformat", ["LIMIT 10", "OFFSET 5"],                                        ("LIMIT", "OFFSET")),
    ("named",    ["OFFSET 5 ROWS", "FETCH NEXT 10 ROWS ONLY"],                    ("OFFSET", "FETCH")),
    ("qmark",    ["OFFSET 5", "LIMIT 10"],                                        ("OFFSET", "LIMIT")),
])
def test_pagination(paramstyle, expected_substrings, order_check):
    sql, _ = build_query(
        "public", "t",
        QueryParams(count=10, start_index=5),
        paramstyle=paramstyle,
    )
    for needle in expected_substrings:
        assert needle in sql, f"missing {needle!r} in {sql!r}"
    first, second = order_check
    assert sql.index(first) < sql.index(second), (
        f"expected {first!r} before {second!r} in {sql!r}"
    )


# --- Simple ?col=val filters use p_-prefixed binds -------------------------


@pytest.mark.parametrize("paramstyle,placeholder,bind_kind", [
    ("pyformat", "%(p_name)s", "dict"),
    ("named",    ":p_name",    "dict"),
    ("qmark",    "?",          "list"),
])
def test_simple_filter_emits_p_prefixed_bind(paramstyle, placeholder, bind_kind):
    sql, binds = build_query(
        "public", "t",
        QueryParams(simple_filters={"name": "Laptop"}),
        paramstyle=paramstyle,
    )
    assert placeholder in sql
    if bind_kind == "dict":
        assert binds["p_name"] == "Laptop"
    else:
        assert binds == ["Laptop"]


@pytest.mark.parametrize("paramstyle,filter_placeholder,simple_placeholder,expected_binds", [
    ("pyformat", "%(f0)s", "%(p_category)s", {"f0": 10, "p_category": "electronics"}),
    ("named",    ":f0",    ":p_category",    {"f0": 10, "p_category": "electronics"}),
    ("qmark",    "?",      "?",              [10, "electronics"]),
])
def test_filter_and_simple_combined(paramstyle, filter_placeholder, simple_placeholder, expected_binds):
    """$filter binds (f0, f1...) and simple-filter binds (p_{col}) coexist.

    For qmark, order is: filter binds first, then simple-filter binds.
    """
    sql, binds = build_query(
        "public", "t",
        QueryParams(filter_="price gt 10", simple_filters={"category": "electronics"}),
        paramstyle=paramstyle,
    )
    assert f"price > {filter_placeholder}" in sql
    assert f"category = {simple_placeholder}" in sql
    assert binds == expected_binds


# --- $groupby + $having (paramstyle-independent grammar, paramstyle-specific binds) ---


@pytest.mark.parametrize("paramstyle,expected_sql", [
    ("pyformat", "SELECT category FROM public.t GROUP BY category"),
    ("named",    "SELECT category FROM PUBLIC.T GROUP BY category"),
    ("qmark",    "SELECT category FROM public.t GROUP BY category"),
])
def test_groupby_no_filter(paramstyle, expected_sql):
    sql, binds = build_query(
        "public", "t",
        QueryParams(select="category", groupby="category"),
        paramstyle=paramstyle,
    )
    assert sql == expected_sql
    assert binds in ({}, [])


@pytest.mark.parametrize("paramstyle,placeholder,expected_binds", [
    ("pyformat", "%(f0)s", {"f0": "books"}),
    ("named",    ":f0",    {"f0": "books"}),
    ("qmark",    "?",      ["books"]),
])
def test_having(paramstyle, placeholder, expected_binds):
    sql, binds = build_query(
        "public", "t",
        QueryParams(select="category", groupby="category", having="category eq 'books'"),
        paramstyle=paramstyle,
    )
    assert f"HAVING category = {placeholder}" in sql
    assert binds == expected_binds


@pytest.mark.parametrize("paramstyle,filter_ph,having_ph,expected_binds", [
    ("pyformat", "%(f0)s", "%(f1)s", {"f0": 10, "f1": "books"}),
    ("named",    ":f0",    ":f1",    {"f0": 10, "f1": "books"}),
    ("qmark",    "?",      "?",      [10, "books"]),
])
def test_filter_groupby_having_share_bind_counter(paramstyle, filter_ph, having_ph, expected_binds):
    """The expression bind counter is shared across $filter and $having so the two
    placeholder series don't collide. qmark assertion verifies positional order.
    """
    sql, binds = build_query(
        "public", "t",
        QueryParams(
            select="category", filter_="price gt 10",
            groupby="category", having="category ne 'books'",
        ),
        paramstyle=paramstyle,
    )
    assert f"WHERE price > {filter_ph}" in sql
    assert f"HAVING category <> {having_ph}" in sql
    assert binds == expected_binds


# --- Validation errors are paramstyle-independent --------------------------


@pytest.mark.parametrize("paramstyle", ["pyformat", "named", "qmark"])
def test_filter_unknown_ident_raises(paramstyle):
    with pytest.raises(ExpressionError, match="Invalid"):
        build_query(
            "public", "t",
            QueryParams(filter_="bogus eq 1"),
            paramstyle=paramstyle,
        )
