"""AST nodes for the closed-grammar ``$filter`` / ``$having`` parser.

One dataclass per grammar production. Separate classes (rather than a
single ``Comparison(op, ...)`` discriminator) keep the downstream walks
— ``emit_sql`` in :mod:`.emit`, ``constrained_columns`` in
:mod:`.analyze` — readable as ``isinstance`` ladders.
"""

from dataclasses import dataclass
from typing import Any

KEYWORDS = {
    "and",
    "or",
    "not",
    "in",
    "eq",
    "ne",
    "gt",
    "ge",
    "lt",
    "le",
    "null",
}


class ExpressionError(ValueError):
    """Raised when an expression fails to parse or validate."""


@dataclass
class Eq:
    col: str
    value: Any


@dataclass
class Ne:
    col: str
    value: Any


@dataclass
class Gt:
    col: str
    value: Any


@dataclass
class Ge:
    col: str
    value: Any


@dataclass
class Lt:
    col: str
    value: Any


@dataclass
class Le:
    col: str
    value: Any


@dataclass
class IsNull:
    """``ident eq null`` → ``ident IS NULL``."""

    col: str


@dataclass
class IsNotNull:
    """``ident ne null`` → ``ident IS NOT NULL``."""

    col: str


@dataclass
class In:
    col: str
    values: list[Any]


@dataclass
class And:
    left: "Node"
    right: "Node"


@dataclass
class Or:
    left: "Node"
    right: "Node"


@dataclass
class Not:
    inner: "Node"


@dataclass
class Paren:
    """Preserved so :func:`.emit.emit_sql` can render ``(...)`` exactly
    where the user wrote them — the SQL output is inspected in tests
    and influences readability of operator-debugging logs."""

    inner: "Node"


Node = Eq | Ne | Gt | Ge | Lt | Le | IsNull | IsNotNull | In | And | Or | Not | Paren


@dataclass
class Token:
    """Lexer output. ``kind`` is one of: ``ident``, ``number``, ``string``,
    ``keyword``, ``lparen``, ``rparen``, ``comma``. ``pos`` is the
    1-based character index used for error messages."""

    kind: str
    value: Any
    pos: int


# Operator-keyword → AST node class (the comparison nodes share shape;
# the parser dispatches via this map).
COMPARISON_NODE = {
    "eq": Eq,
    "ne": Ne,
    "gt": Gt,
    "ge": Ge,
    "lt": Lt,
    "le": Le,
}


# Operator-keyword → SQL operator (used by the emitter).
COMPARISON_SQL = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
}
