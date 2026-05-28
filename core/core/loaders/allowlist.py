"""Allowlist loader — narrow the dynamic surface to a curated set of
schemas + tables.

Loads ``allowlist.yaml`` from the same config root that holds
``validation/`` and ``queries/``. Two top-level sections, both optional;
both support glob patterns:

.. code-block:: yaml

    schemas:
      - public
      - audit

    tables:
      - public.products
      - public.fact_*
      - audit.events

Semantics:

- Both empty / file missing → no narrowing (today's open-by-default
  posture preserved when no allowlist is supplied).
- ``schemas`` non-empty → only listed schemas pass.
- ``tables`` non-empty → only tables matching at least one entry pass
  (combined with ``schemas`` if both are set; AND, not OR).
- Glob patterns use ``fnmatch`` semantics — ``*`` matches any run of
  non-slash characters within one segment.

Replaces the legacy ``ALLOWED_SCHEMAS`` env var. Pre-1.0 means
the env var goes away cleanly; operators move the schema list into
``allowlist.yaml`` under ``schemas:``.

Loaded by lifespan after ``schema_cache.refresh()`` and applied by
``narrow_cache()`` which mutates the cache in place. Re-applied by
``/admin/reload-config`` so operators can adjust the allowlist
without restarts.
"""

import fnmatch
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class Allowlist:
    """Parsed contents of ``allowlist.yaml``.

    Empty schemas + empty tables == "no narrowing" — checked via
    :meth:`is_empty` so callers can short-circuit without iterating.
    """

    def __init__(
        self,
        schemas: list[str] | None = None,
        tables: list[str] | None = None,
    ):
        self.schema_patterns: list[str] = [s.lower() for s in (schemas or [])]
        self.table_patterns: list[str] = [t.lower() for t in (tables or [])]

    def is_empty(self) -> bool:
        """True when no patterns are configured — callers skip narrowing."""
        return not self.schema_patterns and not self.table_patterns

    def schema_allowed(self, schema: str) -> bool:
        """Match a bare schema name against the schema patterns. Returns
        True when no patterns are configured (open allowlist)."""
        if not self.schema_patterns:
            return True
        s = schema.lower()
        return any(fnmatch.fnmatchcase(s, p) for p in self.schema_patterns)

    def table_allowed(self, schema: str, table: str) -> bool:
        """Match ``schema.table`` against the table patterns. Returns True
        when no patterns are configured (open allowlist). Patterns are
        glob-style and matched case-insensitively against the qualified
        name."""
        if not self.table_patterns:
            return True
        qual = f"{schema.lower()}.{table.lower()}"
        return any(fnmatch.fnmatchcase(qual, p) for p in self.table_patterns)


_loaded: Allowlist = Allowlist()


def load(config_root: str) -> Allowlist:
    """Read ``<config_root>/allowlist.yaml`` if present, parse, return.

    Sets the module-level ``_loaded`` so :func:`narrow_cache` (called
    from lifespan and ``/admin/reload-config``) doesn't have to re-read
    the file. Returns an empty ``Allowlist`` when no file exists.
    """
    global _loaded
    path = Path(config_root) / "allowlist.yaml"
    if not path.exists():
        logger.info("No allowlist.yaml found — DDL surface unrestricted by allowlist")
        _loaded = Allowlist()
        return _loaded

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    schemas = data.get("schemas") or []
    tables = data.get("tables") or []
    if not isinstance(schemas, list) or not isinstance(tables, list):
        logger.error(
            "allowlist.yaml — `schemas` and `tables` must be lists; "
            "got schemas=%s tables=%s. Treating as empty.",
            type(schemas).__name__,
            type(tables).__name__,
        )
        _loaded = Allowlist()
        return _loaded

    _loaded = Allowlist(schemas=[str(s) for s in schemas], tables=[str(t) for t in tables])
    logger.info(
        "Loaded allowlist.yaml: schemas=%s tables=%s",
        _loaded.schema_patterns,
        _loaded.table_patterns,
    )
    return _loaded


def get() -> Allowlist:
    """Return the most recently loaded allowlist."""
    return _loaded


def narrow_cache(cache: dict[str, dict[str, set]], *, mode: str = "enforce") -> int:
    """Mutate ``cache`` in place to drop schemas + tables that don't
    pass the allowlist. Returns the count of remaining tables.

    Call this after ``schema_cache.refresh()`` and after each
    ``/admin/reload-config`` so the cache always reflects the current
    allowlist. No-op when the allowlist is empty (today's default
    behavior preserved when no YAML is supplied) **or** when
    ``mode="presentation"`` — in presentation mode the allowlist is a
    spec-only filter and the full DDL surface stays reachable; the
    OpenAPI renderer applies the matchers at render time instead.
    """
    if mode == "presentation":
        # Spec-only filter — leave the cache alone. The renderer
        # walks ``allowlist.get()`` itself when building paths.
        return sum(len(tables) for tables in cache.values())

    if _loaded.is_empty():
        return sum(len(tables) for tables in cache.values())

    schemas_to_drop: list[str] = []
    for schema in cache:
        if not _loaded.schema_allowed(schema):
            schemas_to_drop.append(schema)
            continue
        tables_to_drop = [t for t in cache[schema] if not _loaded.table_allowed(schema, t)]
        for t in tables_to_drop:
            del cache[schema][t]
        if not cache[schema]:
            schemas_to_drop.append(schema)

    for s in schemas_to_drop:
        del cache[s]

    remaining = sum(len(tables) for tables in cache.values())
    logger.info(
        "Allowlist narrowed DDL cache to %d schema(s) / %d table(s)",
        len(cache),
        remaining,
    )
    return remaining
