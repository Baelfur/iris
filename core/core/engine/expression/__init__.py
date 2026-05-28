"""Closed-grammar expression parser for ``$filter`` and ``$having``.

Grammar::

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

**Two-pass architecture**:

1. :func:`.parse.parse_to_ast` — tokenize + recursive descent into
   AST nodes. Validates identifiers as it goes; literals stay as raw
   Python values.
2. :func:`.emit.emit_sql` — walk the AST and produce a SQL fragment,
   binding literals through the supplied ``BindAccumulator``.

The split lets static analyzers (e.g.
:func:`.analyze.constrained_columns`) walk the same AST without
re-parsing or duplicating the grammar.

**Safety properties** held by the parser regardless of caller:

- Every identifier is validated against a caller-supplied validator
  before it reaches emitted SQL. Unknown identifiers raise
  :class:`.ast.ExpressionError`.
- Every literal is emitted as a bind parameter, never interpolated.
- No functions, subqueries, or ident-op-ident comparisons — the
  grammar cannot express them.

**Public surface** (re-exported below): :func:`parse`,
:func:`constrained_columns_in`, :class:`ExpressionError`, plus
:class:`BindAccumulator` and the ``Binds`` type alias from
:mod:`core.engine.paramstyle` for callers that historically
imported them through this module.
"""

from collections.abc import Callable

from ..paramstyle import BindAccumulator, Binds  # noqa: F401 — re-export
from .analyze import constrained_columns, constrained_columns_in  # noqa: F401
from .ast import (  # noqa: F401 — public AST surface for tests/static analyzers
    COMPARISON_NODE,
    COMPARISON_SQL,
    And,
    Eq,
    ExpressionError,
    Ge,
    Gt,
    In,
    IsNotNull,
    IsNull,
    Le,
    Lt,
    Ne,
    Node,
    Not,
    Or,
    Paren,
    Token,
)
from .emit import emit_sql
from .parse import parse_to_ast


def parse(
    text: str,
    validate_ident: Callable[[str], str | None],
    emitter: BindAccumulator,
) -> str:
    """Parse ``text`` and emit SQL. Returns the SQL fragment (no leading WHERE).

    Convenience wrapper around :func:`.parse.parse_to_ast` +
    :func:`.emit.emit_sql` for callers that just want SQL out and don't
    need to inspect the AST. ``validate_ident`` is called for every
    identifier in the expression and should return an error string or
    ``None``. Typically wraps ``schema_cache.validate_columns`` for the
    current schema/view.
    """
    ast = parse_to_ast(text, validate_ident)
    return emit_sql(ast, emitter)
