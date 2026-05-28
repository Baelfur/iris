"""Shared paramstyle-aware bind accumulator.

One container, one method, three callers:

- ``core.expression`` (parsed ``$filter`` / ``$having``): anonymous
  ``bind(value)`` calls produce auto-incrementing ``f0``, ``f1``, ...
  keys.
- ``core.query_engine.build_query`` (simple ``?col=value`` filters):
  ``bind(value, key="p_<col>")`` calls preserve the caller's key.
- ``core.routes.queries._substitute_params`` (custom-query YAML
  ``:name`` placeholders): ``bind(value, key=name)`` calls preserve the
  operator's param name verbatim.

Both modes coexist on a single accumulator. The auto-name counter only
advances on anonymous calls — caller-keyed binds don't bump it, so the
two namespaces (``f{N}`` and ``p_{col}`` / operator-keyed) stay disjoint
without coordination.

For ``qmark``, ``key`` is ignored and the binds container is a positional
list; ordering is the order in which ``bind()`` was called.
"""

from typing import Any

Binds = dict[str, Any] | list[Any]


class BindAccumulator:
    """Accumulates bind values and returns paramstyle-correct placeholders."""

    def __init__(self, paramstyle: str, prefix: str = "f"):
        self.paramstyle = paramstyle
        self.prefix = prefix
        self.binds: Binds = [] if paramstyle == "qmark" else {}
        self._counter = 0

    def bind(self, value: Any, key: str | None = None) -> str:
        """Append ``value`` and return the placeholder.

        ``key`` is used for dict-style paramstyles (``pyformat`` / ``named``).
        ``None`` means autoname using ``{prefix}{counter}`` and bump the
        counter; an explicit key bypasses the counter entirely. Ignored
        under ``qmark`` (positional binds).
        """
        if self.paramstyle == "qmark":
            assert isinstance(self.binds, list)  # noqa: S101 — mypy narrowing
            self.binds.append(value)
            return "?"
        if key is None:
            key = f"{self.prefix}{self._counter}"
            self._counter += 1
        assert isinstance(self.binds, dict)  # noqa: S101 — mypy narrowing
        self.binds[key] = value
        return f":{key}" if self.paramstyle == "named" else f"%({key})s"
