"""SQL emission. AST in → SQL fragment out.

Walks the AST recursively, dispatching on node type. Literals bind
through the supplied :class:`~core.engine.paramstyle.BindAccumulator`,
which knows the right placeholder shape for the variant's paramstyle
(``%(name)s`` for pyformat, ``:name`` for named, ``?`` for qmark) and
collects the bound values into the right container shape (dict for
pyformat / named, list for qmark).

The emitted SQL is a fragment without a leading ``WHERE`` — callers
prepend that. Parens around binary nodes are explicit (``(left AND right)``)
to make the precedence visible in test output and operator-debugging
logs.
"""

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


def emit_sql(node: Node, emitter) -> str:
    """Walk the AST and produce a SQL fragment, binding literals via emitter.

    The ``emitter`` is duck-typed against
    :class:`core.engine.paramstyle.BindAccumulator` — the only call
    is ``emitter.bind(value) -> placeholder_str``. Tests use a
    BindAccumulator instance directly; production code threads one
    through ``build_query``.
    """
    if isinstance(node, Eq):
        return f"{node.col} = {emitter.bind(node.value)}"
    if isinstance(node, Ne):
        return f"{node.col} <> {emitter.bind(node.value)}"
    if isinstance(node, Gt):
        return f"{node.col} > {emitter.bind(node.value)}"
    if isinstance(node, Ge):
        return f"{node.col} >= {emitter.bind(node.value)}"
    if isinstance(node, Lt):
        return f"{node.col} < {emitter.bind(node.value)}"
    if isinstance(node, Le):
        return f"{node.col} <= {emitter.bind(node.value)}"
    if isinstance(node, IsNull):
        return f"{node.col} IS NULL"
    if isinstance(node, IsNotNull):
        return f"{node.col} IS NOT NULL"
    if isinstance(node, In):
        bound = ", ".join(emitter.bind(v) for v in node.values)
        return f"{node.col} IN ({bound})"
    if isinstance(node, And):
        return f"({emit_sql(node.left, emitter)} AND {emit_sql(node.right, emitter)})"
    if isinstance(node, Or):
        return f"({emit_sql(node.left, emitter)} OR {emit_sql(node.right, emitter)})"
    if isinstance(node, Not):
        return f"(NOT {emit_sql(node.inner, emitter)})"
    if isinstance(node, Paren):
        return f"({emit_sql(node.inner, emitter)})"
    raise AssertionError(f"unknown node type: {type(node).__name__}")  # pragma: no cover
