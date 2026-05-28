"""Tests for core.expression — the closed-grammar $filter parser.

Parametrized across all three paramstyles so the BindAccumulator's
placeholder output is verified in one place instead of being duplicated
per variant.
"""

import pytest

from core.engine.expression import BindAccumulator, ExpressionError, parse


def _run(text, paramstyle, ident_validator):
    emitter = BindAccumulator(paramstyle)
    sql = parse(text, ident_validator, emitter)
    return sql, emitter.binds


# --- Placeholder shape per paramstyle ---

@pytest.mark.parametrize("paramstyle,placeholder", [
    ("pyformat", "%(f0)s"),
    ("named", ":f0"),
    ("qmark", "?"),
])
def test_placeholder_shape_per_paramstyle(paramstyle, placeholder, ident_validator):
    sql, _ = _run("id eq 1", paramstyle, ident_validator)
    assert placeholder in sql


def test_qmark_binds_are_a_list(ident_validator):
    sql, binds = _run("id eq 1 and name eq 'x'", "qmark", ident_validator)
    assert sql.count("?") == 2
    assert binds == [1, "x"]


def test_pyformat_binds_are_a_dict(ident_validator):
    _, binds = _run("id eq 1 and name eq 'x'", "pyformat", ident_validator)
    assert binds == {"f0": 1, "f1": "x"}


def test_named_binds_are_a_dict(ident_validator):
    _, binds = _run("id eq 1 and name eq 'x'", "named", ident_validator)
    assert binds == {"f0": 1, "f1": "x"}


# --- Operators ---

@pytest.mark.parametrize("op,sql_op", [
    ("eq", "="),
    ("ne", "<>"),
    ("gt", ">"),
    ("ge", ">="),
    ("lt", "<"),
    ("le", "<="),
])
def test_comparison_operators(op, sql_op, ident_validator):
    sql, _ = _run(f"price {op} 10", "pyformat", ident_validator)
    assert f"price {sql_op} %(f0)s" in sql


def test_string_literal(ident_validator):
    _, binds = _run("name eq 'Laptop'", "pyformat", ident_validator)
    assert binds["f0"] == "Laptop"


def test_string_with_embedded_quote(ident_validator):
    _, binds = _run("name eq 'O''Brien'", "pyformat", ident_validator)
    assert binds["f0"] == "O'Brien"


def test_negative_number(ident_validator):
    _, binds = _run("price gt -5", "pyformat", ident_validator)
    assert binds["f0"] == -5


def test_decimal_number(ident_validator):
    _, binds = _run("price gt 3.14", "pyformat", ident_validator)
    assert binds["f0"] == 3.14


# --- Null handling ---

def test_null_eq_becomes_is_null(ident_validator):
    sql, binds = _run("status eq null", "pyformat", ident_validator)
    assert sql == "status IS NULL"
    assert binds == {}


def test_null_ne_becomes_is_not_null(ident_validator):
    sql, _ = _run("status ne null", "pyformat", ident_validator)
    assert sql == "status IS NOT NULL"


def test_null_with_other_op_rejected(ident_validator):
    with pytest.raises(ExpressionError):
        _run("price gt null", "pyformat", ident_validator)


# --- IN lists ---

def test_in_list(ident_validator):
    sql, binds = _run("id in (1, 2, 3)", "pyformat", ident_validator)
    assert "id IN (%(f0)s, %(f1)s, %(f2)s)" in sql
    assert binds == {"f0": 1, "f1": 2, "f2": 3}


def test_in_list_strings(ident_validator):
    _, binds = _run("category in ('books', 'media')", "pyformat", ident_validator)
    assert binds == {"f0": "books", "f1": "media"}


# --- Boolean combinators ---

def test_not(ident_validator):
    sql, _ = _run("not id eq 1", "pyformat", ident_validator)
    assert sql.startswith("(NOT")


def test_and_binds_tighter_than_or(ident_validator):
    sql, _ = _run("id eq 1 or id eq 2 and id eq 3", "pyformat", ident_validator)
    assert sql.index("AND") > sql.index("OR")


def test_parens_override_precedence(ident_validator):
    sql, _ = _run("(id eq 1 or id eq 2) and price gt 10", "pyformat", ident_validator)
    assert "OR" in sql and "AND" in sql


def test_case_insensitive_keywords_and_idents(ident_validator):
    sql, _ = _run("ID EQ 1 AND Name NE 'x'", "pyformat", ident_validator)
    assert "id = %(f0)s" in sql
    assert "name <> %(f1)s" in sql


# --- Error cases ---

def test_unknown_ident_rejected(ident_validator):
    with pytest.raises(ExpressionError, match="Invalid"):
        _run("bogus eq 1", "pyformat", ident_validator)


def test_legacy_sql_syntax_rejected(ident_validator):
    with pytest.raises(ExpressionError):
        _run("id = 1", "pyformat", ident_validator)


def test_empty_expression_rejected(ident_validator):
    with pytest.raises(ExpressionError):
        _run("", "pyformat", ident_validator)


def test_trailing_garbage_rejected(ident_validator):
    with pytest.raises(ExpressionError):
        _run("id eq 1 bogus", "pyformat", ident_validator)


def test_missing_operator_rejected(ident_validator):
    with pytest.raises(ExpressionError):
        _run("id", "pyformat", ident_validator)


def test_unterminated_string_rejected(ident_validator):
    with pytest.raises(ExpressionError):
        _run("name eq 'unterminated", "pyformat", ident_validator)


def test_ident_op_ident_rejected(ident_validator):
    """Can't compare two columns — grammar only allows ident op literal."""
    with pytest.raises(ExpressionError):
        _run("id eq name", "pyformat", ident_validator)


def test_function_call_rejected(ident_validator):
    """No function calls in the grammar — identifier can't be followed by (."""
    with pytest.raises(ExpressionError):
        _run("trim(name) eq 'x'", "pyformat", ident_validator)


def test_error_includes_position(ident_validator):
    with pytest.raises(ExpressionError, match="position"):
        _run("id eq 1 bogus", "pyformat", ident_validator)


# --- constrained_columns: strict equality-constraint analysis (#187) ---

from core.engine.expression import constrained_columns_in


@pytest.mark.parametrize("text,expected", [
    # Eq / In contribute their column.
    ("id eq 1", {"id"}),
    ("id in (1, 2, 3)", {"id"}),
    ("name eq 'Laptop'", {"name"}),
    # Range/exclusion/predicate operators do NOT contribute.
    ("id ne 1", set()),
    ("id gt 0", set()),
    ("id ge 0", set()),
    ("id lt 100", set()),
    ("id le 100", set()),
    ("id eq null", set()),
    ("id ne null", set()),
    # Negation collapses the inner result.
    ("not (id eq 1)", set()),
    # AND unions both branches — either side suffices.
    ("id eq 1 and category eq 'foo'", {"id", "category"}),
    ("id eq 1 and category gt 'a'", {"id"}),
    ("id gt 0 and category eq 'foo'", {"category"}),
    # OR intersects — must hold on both sides.
    ("id eq 1 or category eq 'foo'", set()),
    ("id eq 1 or id eq 2", {"id"}),
    ("id eq 1 or id in (2, 3)", {"id"}),
    # Parens are transparent.
    ("(id eq 1)", {"id"}),
    ("(id eq 1 and category eq 'foo')", {"id", "category"}),
    # Mixed: AND wraps an OR — the AND-side constrains.
    ("id eq 1 and (category gt 'a' or name eq 'foo')", {"id"}),
    # OR with one side having more constraints — only the intersection counts.
    ("(id eq 1 and name eq 'a') or id eq 2", {"id"}),
])
def test_constrained_columns_strict_semantics(text, expected, ident_validator):
    assert constrained_columns_in(text, ident_validator) == expected


def test_constrained_columns_empty_input(ident_validator):
    assert constrained_columns_in("", ident_validator) == set()
    assert constrained_columns_in(None, ident_validator) == set()
    assert constrained_columns_in("   ", ident_validator) == set()


def test_constrained_columns_validates_idents(ident_validator):
    """Identifier validation runs during parse, surfaces as ExpressionError."""
    with pytest.raises(ExpressionError):
        constrained_columns_in("bogus eq 1", ident_validator)
