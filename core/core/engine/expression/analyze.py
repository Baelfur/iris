"""Static analysis over the AST. No SQL emission, no parser state.

Currently exposes :func:`constrained_columns` (and the convenience
:func:`constrained_columns_in` that takes raw text) — used by the view-
def required-param check to decide whether ``$filter`` proves a
column is equality-constrained on every matching row.

Lives in its own module because the analysis walks the AST without
emitting SQL, so it shouldn't drag the emitter or the
``BindAccumulator`` into modules that only care about static analysis.
"""

from collections.abc import Callable

from .ast import (
    And,
    Eq,
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
)
from .parse import parse_to_ast


def constrained_columns(node: Node) -> set[str]:
    """Return the set of columns that any matching row is **guaranteed**
    to be equality-constrained on.

    Strict semantics — the column must appear in an ``eq`` or ``in``
    constraint at a position where every matching row honors it:

    - ``Eq``, ``In`` contribute their column.
    - ``Ne``, ``Gt`` / ``Ge`` / ``Lt`` / ``Le``, ``IsNull``,
      ``IsNotNull`` do NOT — they describe ranges, exclusions, or
      predicates without a specific value.
    - ``Not`` collapses the inner result to ``set()`` — negation can't
      guarantee the constraint.
    - ``And`` unions both branches (either side suffices).
    - ``Or`` intersects both branches (the constraint must hold
      whichever side matches).
    - ``Paren`` is transparent.

    Used by ``ViewDef.satisfied_by`` to decide whether ``$filter``
    contributes toward a YAML-declared required column.
    """
    if isinstance(node, (Eq, In)):
        return {node.col}
    if isinstance(node, (Ne, Gt, Ge, Lt, Le, IsNull, IsNotNull)):
        return set()
    if isinstance(node, Not):
        return set()
    if isinstance(node, And):
        return constrained_columns(node.left) | constrained_columns(node.right)
    if isinstance(node, Or):
        return constrained_columns(node.left) & constrained_columns(node.right)
    if isinstance(node, Paren):
        return constrained_columns(node.inner)
    raise AssertionError(f"unknown node type: {type(node).__name__}")  # pragma: no cover


def constrained_columns_in(
    text: str,
    validate_ident: Callable[[str], str | None],
) -> set[str]:
    """Convenience: parse ``text`` and return the equality-constrained
    column set.

    Empty / whitespace / ``None`` input returns an empty set — a missing
    ``$filter`` doesn't constrain anything. The route uses this when it
    has the raw ``$filter`` string and just wants the column set, not
    a re-emittable AST.
    """
    if not text or not text.strip():
        return set()
    return constrained_columns(parse_to_ast(text, validate_ident))
