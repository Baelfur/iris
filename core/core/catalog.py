"""Catalog enumeration — single source of truth for walking the
harvested catalog.

Used by both ``/admin/catalog`` (operator-facing JSON dump) and
``openapi_dynamic`` (per-deployment OpenAPI spec). previously each
caller iterated the same DDL cache + view-def + custom-query state
with its own loop; when the catalog gains a new dimension (row-level
policies, write-mode flags, tags, etc.) both call sites had to
update in lockstep. This module collapses the iteration shape to
one place; callers project the entries into whatever output dict
they need.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from .engine import schema_cache
from .loaders import queries as custom_queries
from .loaders import validation as view_defs
from .loaders.contract import ParamContract
from .loaders.queries import QueryDef


@dataclass(frozen=True)
class TableEntry:
    """One harvested table with everything callers need to project.

    ``view_def`` is ``None`` when no validation YAML covers this table.
    ``indexed_columns`` is the set of PK / FK / indexed columns the
    variant's DDL harvest reported — used by the spec renderer in
    ``optimized-schema`` mode; safe to ignore otherwise. Empty set
    when the variant doesn't expose index metadata (Trino).
    """

    schema: str
    table: str
    columns: set[str]
    view_def: ParamContract | None
    indexed_columns: set[str]


@dataclass(frozen=True)
class QueryEntry:
    """One custom query loaded from YAML, paired with its registered
    URL path. ``path`` is what each consumer renders into the URL
    surface (``/queries/<path>``); the ``qdef`` carries the SQL,
    parameter contract, and aliases."""

    path: str
    qdef: QueryDef


def iter_tables() -> Iterator[TableEntry]:
    """Yield every harvested table with its view-def + indexed-column
    metadata. Driven by the DDL cache; reflects whatever
    ``schema_cache.refresh()`` last produced (post-allowlist narrowing
    when ``allowlist.yaml`` is set)."""
    cache = schema_cache.get_cache()
    for schema_name, tables in cache.items():
        for table_name, columns in tables.items():
            yield TableEntry(
                schema=schema_name,
                table=table_name,
                columns=columns,
                view_def=view_defs.get_def(schema_name, table_name),
                indexed_columns=schema_cache.get_indexed_columns(schema_name, table_name),
            )


def iter_queries() -> Iterator[QueryEntry]:
    """Yield every loaded custom query in registration order. Skips
    paths whose ``QueryDef`` couldn't be retrieved — defensive against
    a race between ``list_queries()`` and ``get_query()``, though the
    loaders make this extremely unlikely in practice."""
    for path in custom_queries.list_queries():
        qdef = custom_queries.get_query(path)
        if qdef is not None:
            yield QueryEntry(path=path, qdef=qdef)
