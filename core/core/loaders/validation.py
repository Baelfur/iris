"""YAML view definitions — optional param constraints for dynamic table endpoints.

Place YAML files in the validation/ directory following the schema/table structure:

    validation/
      crm/
        customers.yaml

A view definition looks like:

    params:
      required:
        - customer_id
      optional:
        - status
        - city
    aliases:
      - /legacy/crm/customers   # legacy URL kept alive during migration
      - /legacy/inventory/sites      # different prefix, same handler

- No YAML file = current behavior (all columns allowed, no required params)
- required: params that MUST be supplied — prevents full table dumps
- optional: additional params that CAN be supplied
- If a YAML exists, only required + optional params are accepted
- aliases (optional): list of additional URL paths that route to the same
  handler. Used for migrating consumers off legacy URL surfaces (data-
  virtualization layers, internal proxies, vendor APIs being replaced)
  without forcing a
  coordinated cutover. Each alias becomes a FastAPI route alongside the
  canonical `/{schema}/{table}` route.

The shape of a parsed view definition is a :class:`ParamContract` —
shared with :mod:`core.loaders.queries` so the custom-query
loader doesn't depend on this module to describe its own param
contract. ``ViewDef`` is kept as an alias for back-compat with callers
that imported it directly. Aliases are tracked in a parallel registry
so ``ParamContract`` stays focused on parameter validation only.
"""

import logging
from pathlib import Path

import yaml

from .contract import ParamContract

logger = logging.getLogger(__name__)

# Back-compat alias — the YAML format is "view definitions"; the underlying
# shape is :class:`ParamContract` (shared with the custom-query loader).
ViewDef = ParamContract

# schema -> table -> ParamContract
_defs: dict[str, dict[str, ParamContract]] = {}

# (schema, table) -> list of alias paths. Parallel registry to _defs so
# ParamContract doesn't have to carry routing-metadata it doesn't use.
_aliases: dict[tuple[str, str], list[str]] = {}


def load_views(validation_dir: str = "validation") -> int:
    """Scan validation/ directory for YAML definitions.

    Returns count of definitions loaded.
    """
    global _defs, _aliases
    _defs = {}
    _aliases = {}
    base = Path(validation_dir)

    if not base.exists():
        logger.info("No validation/ directory found — all tables unrestricted")
        return 0

    count = 0
    for yml in base.rglob("*.yaml"):
        parts = yml.relative_to(base).parts
        if len(parts) != 2:
            continue
        schema = parts[0].lower()
        table = yml.stem.lower()

        with open(yml) as f:
            data = yaml.safe_load(f) or {}

        params = data.get("params", {})
        vdef = ParamContract(
            required=params.get("required", []),
            optional=params.get("optional", []),
        )
        _defs.setdefault(schema, {})[table] = vdef

        aliases = data.get("aliases") or []
        if not isinstance(aliases, list):
            logger.warning(
                "validation/%s/%s.yaml — 'aliases' must be a list; got %s. Skipping aliases.",
                schema,
                table,
                type(aliases).__name__,
            )
            aliases = []
        if aliases:
            _aliases[(schema, table)] = [str(a) for a in aliases]

        count += 1
        logger.info(
            "Loaded view def: %s.%s (required=%s, optional=%s, aliases=%s)",
            schema,
            table,
            sorted(vdef.required),
            sorted(vdef.optional),
            _aliases.get((schema, table), []),
        )

    return count


def get_def(schema: str, table: str) -> ParamContract | None:
    """Get the view definition for a schema.table, or None if unrestricted."""
    return _defs.get(schema.lower(), {}).get(table.lower())


def all_aliases() -> list[tuple[str, str, str]]:
    """Iterate every (schema, table, alias_path) triple loaded from
    validation YAMLs. Used at lifespan startup to register one
    FastAPI route per alias.
    """
    return [
        (schema, table, alias) for (schema, table), aliases in _aliases.items() for alias in aliases
    ]


def warn_mismatches(schema_cache_data: dict) -> int:
    """Log warnings for YAML definitions that don't match any DDL table.

    Args:
        schema_cache_data: The DDL schema cache (schema -> table -> columns).

    Returns:
        Count of mismatches found.
    """
    mismatches = 0
    for schema, tables in _defs.items():
        for table in tables:
            if schema not in schema_cache_data:
                logger.warning(
                    "validation/%s/%s.yaml — schema '%s' not found in database",
                    schema,
                    table,
                    schema,
                )
                mismatches += 1
            elif table not in schema_cache_data.get(schema, {}):
                logger.warning(
                    "validation/%s/%s.yaml — table '%s' not found in schema '%s'",
                    schema,
                    table,
                    table,
                    schema,
                )
                mismatches += 1
    return mismatches
