"""Recursive-descent parser. Tokens in → AST out.

Grammar (also documented at the package ``__init__`` docstring)::

    expr       := or-expr
    or-expr    := and-expr ("or" and-expr)*
    and-expr   := not-expr ("and" not-expr)*
    not-expr   := "not"? primary
    primary    := "(" expr ")" | comparison | in-expr
    comparison := ident ("eq"|"ne") (literal | "null")
                | ident ("gt"|"ge"|"lt"|"le") literal
    in-expr    := ident "in" "(" literal ("," literal)* ")"
    ident      := [a-zA-Z_][a-zA-Z0-9_]*
    literal    := number | single-quoted-string

``null`` is only valid on the right side of ``eq`` / ``ne`` (lowered to
``IS NULL`` / ``IS NOT NULL`` by :mod:`.emit`). Boolean literals are
intentionally not supported — clients use the literal the column
actually stores (``1`` / ``0``, ``'Y'`` / ``'N'``, etc.) to stay
driver-agnostic.

Identifiers are validated as the parser walks them — the caller passes
a ``validate(ident) -> Optional[str]`` callback (typically wrapping
``schema_cache.validate_columns`` for the current schema/view). An
unknown identifier raises :class:`.ast.ExpressionError` before any
SQL is emitted.

Literals are kept as raw Python values on the AST nodes (``int``,
``float``, ``str``); binding to placeholders happens in
:mod:`.emit`.
"""

from collections.abc import Callable
from typing import Any

from .ast import (
    COMPARISON_NODE,
    And,
    ExpressionError,
    In,
    IsNotNull,
    IsNull,
    Node,
    Not,
    Or,
    Paren,
    Token,
)
from .tokenize import tokenize


def parse_to_ast(
    text: str,
    validate_ident: Callable[[str], str | None],
) -> Node:
    """Tokenize + recursive descent. Returns an AST. No SQL emission.

    The single public entry into the parser. Callers that want SQL out
    chain ``parse_to_ast`` → :func:`.emit.emit_sql`; callers that want
    static analysis (e.g. :func:`.analyze.constrained_columns`) use the
    AST directly. Both paths share the same parser state, so the
    grammar's safety properties (validated idents, no functions, no
    subqueries) hold regardless of which downstream consumes the AST.
    """
    text = (text or "").strip()
    if not text:
        raise ExpressionError("expression is empty")
    tokens = tokenize(text)
    node, pos = _parse_or(tokens, 0, validate_ident)
    if pos < len(tokens):
        tok = tokens[pos]
        raise ExpressionError(f"unexpected token '{tok.value}' at position {tok.pos}")
    return node


def _peek(tokens: list[Token], pos: int) -> Token | None:
    return tokens[pos] if pos < len(tokens) else None


def _expect_kind(tokens: list[Token], pos: int, kind: str) -> tuple[Token, int]:
    tok = _peek(tokens, pos)
    if tok is None or tok.kind != kind:
        where = f"'{tok.value}' at position {tok.pos}" if tok else "end of expression"
        raise ExpressionError(f"expected {kind}, got {where}")
    return tok, pos + 1


def _parse_or(tokens, pos, validate) -> tuple[Node, int]:
    left, pos = _parse_and(tokens, pos, validate)
    while True:
        tok = _peek(tokens, pos)
        if tok and tok.kind == "keyword" and tok.value == "or":
            pos += 1
            right, pos = _parse_and(tokens, pos, validate)
            left = Or(left, right)
        else:
            return left, pos


def _parse_and(tokens, pos, validate) -> tuple[Node, int]:
    left, pos = _parse_not(tokens, pos, validate)
    while True:
        tok = _peek(tokens, pos)
        if tok and tok.kind == "keyword" and tok.value == "and":
            pos += 1
            right, pos = _parse_not(tokens, pos, validate)
            left = And(left, right)
        else:
            return left, pos


def _parse_not(tokens, pos, validate) -> tuple[Node, int]:
    tok = _peek(tokens, pos)
    if tok and tok.kind == "keyword" and tok.value == "not":
        pos += 1
        inner, pos = _parse_primary(tokens, pos, validate)
        return Not(inner), pos
    return _parse_primary(tokens, pos, validate)


def _parse_primary(tokens, pos, validate) -> tuple[Node, int]:
    tok = _peek(tokens, pos)
    if tok is None:
        raise ExpressionError("unexpected end of expression")
    if tok.kind == "lparen":
        pos += 1
        inner, pos = _parse_or(tokens, pos, validate)
        _, pos = _expect_kind(tokens, pos, "rparen")
        return Paren(inner), pos
    if tok.kind != "ident":
        raise ExpressionError(f"expected identifier, got '{tok.value}' at position {tok.pos}")
    ident = tok.value
    err = validate(ident)
    if err:
        raise ExpressionError(err)
    pos += 1

    op_tok = _peek(tokens, pos)
    if op_tok is None:
        raise ExpressionError(f"expected operator after identifier '{ident}'")
    if op_tok.kind != "keyword":
        raise ExpressionError(
            f"expected comparison operator or 'in' after identifier "
            f"'{ident}', got '{op_tok.value}' at position {op_tok.pos}"
        )
    if op_tok.value == "in":
        return _parse_in_list(tokens, pos + 1, ident)
    if op_tok.value in COMPARISON_NODE:
        return _parse_comparison(tokens, pos + 1, ident, op_tok.value)
    raise ExpressionError(
        f"expected comparison operator or 'in' after identifier "
        f"'{ident}', got '{op_tok.value}' at position {op_tok.pos}"
    )


def _parse_comparison(tokens, pos, ident, op) -> tuple[Node, int]:
    lit_tok = _peek(tokens, pos)
    if lit_tok and lit_tok.kind == "keyword" and lit_tok.value == "null":
        pos += 1
        if op == "eq":
            return IsNull(ident), pos
        if op == "ne":
            return IsNotNull(ident), pos
        raise ExpressionError(f"operator '{op}' cannot be applied to null")
    value, pos = _parse_literal(tokens, pos)
    return COMPARISON_NODE[op](ident, value), pos  # type: ignore[return-value]


def _parse_in_list(tokens, pos, ident) -> tuple[Node, int]:
    _, pos = _expect_kind(tokens, pos, "lparen")
    values: list[Any] = []
    while True:
        value, pos = _parse_literal(tokens, pos)
        values.append(value)
        nxt = _peek(tokens, pos)
        if nxt and nxt.kind == "comma":
            pos += 1
            continue
        break
    _, pos = _expect_kind(tokens, pos, "rparen")
    if not values:
        raise ExpressionError(f"'in' requires at least one value for identifier '{ident}'")
    return In(ident, values), pos


def _parse_literal(tokens, pos) -> tuple[Any, int]:
    """Return the raw literal value (int/float/str), not a SQL placeholder.

    Binding to a paramstyle-specific placeholder happens in
    :func:`.emit.emit_sql`; the AST stays placeholder-free so static
    analyzers can walk it without an emitter in hand.
    """
    tok = _peek(tokens, pos)
    if tok is None:
        raise ExpressionError("expected literal, got end of expression")
    if tok.kind in ("string", "number"):
        return tok.value, pos + 1
    raise ExpressionError(f"expected literal, got '{tok.value}' at position {tok.pos}")
