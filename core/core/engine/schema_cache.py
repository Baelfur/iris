"""DDL schema cache and validation. Harvesting is delegated to the variant's db module."""

import difflib

from ..context import get_context

SchemaMap = dict[str, dict[str, set[str]]]

# Threshold for did-you-mean suggestions on 404/422 paths.
# 0.6 is conservative — "produts" → "products" matches; unrelated tokens
# don't. Tune downward if real misuse shows the bar is too high.
_SUGGEST_CUTOFF = 0.6
_cache: SchemaMap = {}

# Per-table indexed/PK/FK column set, populated by variants whose
# harvest_ddl returns the enriched shape. Used by openapi_render_mode=
# optimized-schema to surface only the indexed columns as
# concrete simple-filter params. Empty by default — the simple-filter param
# ships without per-variant DDL harvest changes; phase C lights this
# up per variant.
_indexes: dict[str, dict[str, set[str]]] = {}


async def refresh() -> int:
    """Re-harvest DDL via the variant's harvest_ddl() and update the cache.

    Variants return either:
    - ``SchemaMap`` (today's shape; column-name only) — ``_indexes``
      stays empty for that variant.
    - ``(SchemaMap, IndexMap)`` tuple — second element populates the
      per-table indexed-column set for openapi_render_mode=
      optimized-schema.

    Returns table count.
    """
    global _cache, _indexes
    result = await get_context().harvest_ddl()
    if isinstance(result, tuple):
        _cache, _indexes = result
    else:
        _cache = result
        _indexes = {}
    return sum(len(tables) for tables in _cache.values())


def get_cache() -> SchemaMap:
    """Return the in-memory DDL map: ``{schema: {table: {column, ...}}}``.

    Read-only view for callers that need to enumerate the surface — the
    admin pool-sizing report, view-def mismatch warnings, etc. Mutators
    use ``refresh()``; everyone else does point lookups via
    ``validate_table`` / ``validate_columns``.
    """
    return _cache


def get_indexed_columns(schema: str, table: str) -> set[str]:
    """Return the set of indexed/PK/FK columns for ``schema.table``,
    or empty set when the variant didn't supply index info.

    Used by openapi_render_mode=optimized-schema to surface
    only the columns the DB can serve efficiently as concrete
    simple-filter params. Variants whose harvest_ddl doesn't return
    index metadata yield empty here; optimized-schema rendering
    degrades to simple-schema layout for those tables.
    """
    return _indexes.get(schema.lower(), {}).get(table.lower(), set())


def _suggest(token: str, candidates) -> str | None:
    """Return the closest match to ``token`` from ``candidates`` or
    ``None`` when no candidate is close enough.

    Used by validators to attach ``did_you_mean`` hints to 404/422
    responses. The threshold is tuned conservative — typos
    suggest, unrelated tokens don't.
    """
    matches = difflib.get_close_matches(
        token.lower(),
        [str(c).lower() for c in candidates],
        n=1,
        cutoff=_SUGGEST_CUTOFF,
    )
    return matches[0] if matches else None


def validate_table(schema: str, table: str) -> dict | None:
    """Check that ``schema.table`` exists in the cache (case-insensitive).

    Returns ``None`` when both schema and table resolve. Otherwise
    returns ``{"message": str}`` plus an optional ``"did_you_mean"``
    suggestion when the typo matches a known schema or table closely
    enough. Call sites pass the dict as the HTTPException detail.
    """
    s, t = schema.lower(), table.lower()
    if s not in _cache:
        err: dict = {"message": f"Schema '{schema}' not found in DDL cache"}
        suggestion = _suggest(schema, _cache.keys())
        if suggestion:
            err["did_you_mean"] = suggestion
        return err
    if t not in _cache[s]:
        err = {"message": f"Table '{table}' not found in schema '{schema}'"}
        suggestion = _suggest(table, _cache[s].keys())
        if suggestion:
            err["did_you_mean"] = suggestion
        return err
    return None


def validate_columns(schema: str, table: str, columns: list[str]) -> dict | None:
    """Check every column in ``columns`` exists on ``schema.table``.

    Case-insensitive; tokens are stripped before comparison. Returns
    ``None`` when all resolve. Otherwise returns ``{"message": str}``
    listing the unknown columns, plus an optional ``"did_you_mean"``
    list of suggestions (parallel order — one suggestion per bad
    column, ``None`` where no close match exists). Caller tokenizes
    the URL grammar surface via ``parse_column_list`` /
    ``parse_orderby_list`` first.
    """
    valid = _cache.get(schema.lower(), {}).get(table.lower(), set())
    bad = [c for c in columns if c.strip().lower() not in valid]
    if not bad:
        return None
    err: dict = {"message": f"Invalid column(s): {', '.join(bad)}"}
    suggestions = [_suggest(c.strip(), valid) for c in bad]
    if any(suggestions):
        err["did_you_mean"] = suggestions
    return err


def parse_column_list(expr: str) -> list[str]:
    """Split a comma-separated column list (``$select`` / ``$groupby``)
    into individual column names. ``*`` and empty tokens are dropped;
    only the first whitespace-separated word per comma-segment is kept,
    so ``id ASC`` doesn't slip through (``parse_orderby_list`` handles
    direction-bearing forms).
    """
    cols = []
    for part in expr.split(","):
        token = part.strip().split()[0].strip()
        if token and token != "*":
            cols.append(token)
    return cols


def parse_orderby_list(expr: str) -> list[tuple]:
    """Parse an $orderby string into [(col, direction)] tuples.

    Direction is "ASC", "DESC", or "" (unset). Anything past column + direction
    is rejected — the result is what's safe to re-emit into SQL after the
    callers validate ``col`` against the DDL cache.
    """
    items: list[tuple] = []
    for part in expr.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        col = tokens[0]
        if col == "*":
            continue
        direction = ""
        if len(tokens) > 1:
            d = tokens[1].upper()
            if d not in ("ASC", "DESC"):
                raise ValueError(f"invalid order direction: {tokens[1]!r}")
            direction = d
        if len(tokens) > 2:
            raise ValueError(f"unexpected tokens after column in $orderby: {part!r}")
        items.append((col, direction))
    return items
