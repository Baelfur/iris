"""Async PostgreSQL connection pool, query helpers, and DDL harvest."""

from typing import Dict, List, Optional, Set

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core.errors.exceptions import DatabaseError
from core.observability.tracing import try_instrument_psycopg

from .config import settings

SchemaMap = Dict[str, Dict[str, Set[str]]]

# System schemas excluded from DDL harvest. Anything outside this set
# that the service account has SELECT on becomes part of the dynamic
# surface; role grants are the canonical scope. The allowlist YAML
# (#269) narrows the post-harvest cache further when supplied.
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

pool: Optional[AsyncConnectionPool] = None


async def _configure_connection(conn) -> None:
    """Apply per-connection session settings (run once per new pool conn).

    Sets ``statement_timeout`` from QUERY_TIMEOUT_SECONDS so a runaway
    query can't hold a pool worker indefinitely. (#17)

    psycopg-pool requires the configure callback to leave the connection
    in a clean state (no open transaction). The ``SET`` runs inside an
    implicit transaction by default, so we commit to release the
    connection cleanly. Without the commit, psycopg-pool 3.3+ logs
    "connection left in status INTRANS by configure function" and
    discards the connection.
    """
    timeout_ms = settings.pool.query_timeout_seconds * 1000
    if timeout_ms > 0:
        async with conn.cursor() as cur:
            await cur.execute(f"SET statement_timeout = {timeout_ms}")
        await conn.commit()


async def init_pool() -> None:
    """Create the async PostgreSQL connection pool."""
    global pool
    # Register OpenTelemetry instrumentation BEFORE the pool opens so
    # the connection-create path is instrumented from the first
    # checkout. No-op when ENABLE_DB_TRACING=false or the OTLP endpoint
    # isn't set; best-effort when the instrumentor package isn't
    # installed. (#250)
    try_instrument_psycopg(settings)
    conninfo = "host={} port={} user={} password={} dbname={}".format(
        settings.pg_host,
        settings.pg_port,
        settings.pg_user,
        settings.pg_password,
        settings.pg_database,
    )
    pool = AsyncConnectionPool(
        conninfo=conninfo,
        min_size=settings.pool.min_size,
        max_size=settings.pool.max_size,
        configure=_configure_connection,
        open=False,
    )
    await pool.open()


async def close_pool() -> None:
    """Close the connection pool and release resources."""
    global pool
    if pool:
        await pool.close(timeout=5)
        pool = None


def get_pool():
    """Return the current connection pool."""
    return pool


async def fetch_all(sql: str, params: Optional[Dict] = None) -> List[Dict]:
    """Execute a SQL query and return all rows as a list of dicts.

    Wraps psycopg.DatabaseError as DatabaseError for driver-agnostic handling.
    """
    if not pool:
        raise RuntimeError("Connection pool not initialized")
    try:
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()
    except psycopg.DatabaseError as exc:
        raise DatabaseError(str(exc).strip()) from exc


async def fetch_all_with_creds(
    sql: str,
    params: Optional[Dict],
    creds: tuple[str, str],
) -> List[Dict]:
    """Execute a query using per-request credentials (one-off connection).

    Credentials flow as kwargs to ``psycopg.AsyncConnection.connect`` — never
    interpolated into a conninfo string. A passthrough caller embedding
    libpq keywords in their username (e.g. ``alice dbname=other_db``) cannot
    redirect the connection: the driver treats the value as the literal user
    field and rejects it at auth time.
    """
    try:
        async with await psycopg.AsyncConnection.connect(
            host=settings.pg_host,
            port=settings.pg_port,
            user=creds[0],
            password=creds[1],
            dbname=settings.pg_database,
        ) as conn:
            await _configure_connection(conn)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()
    except psycopg.DatabaseError as exc:
        raise DatabaseError(str(exc).strip()) from exc


async def ping() -> None:
    """Readiness check — acquire a pool connection and run SELECT 1.

    Raises psycopg.DatabaseError or the asyncio cancellation exception if
    the caller's timeout fires. Readiness route maps both to 503.
    """
    if not pool:
        raise RuntimeError("Connection pool not initialized")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()


async def get_connection_limit() -> tuple[Optional[int], str]:
    """Read the server's max_connections from pg_settings. (#63)"""
    if not pool:
        raise RuntimeError("Connection pool not initialized")
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT setting::int FROM pg_settings WHERE name = 'max_connections'"
                )
                row = await cur.fetchone()
                if row and row[0] is not None:
                    return int(row[0]), "Postgres pg_settings.max_connections"
                return None, "Postgres pg_settings.max_connections (no row)"
    except psycopg.DatabaseError as exc:
        return None, f"Postgres pg_settings read failed: {type(exc).__name__}"


async def harvest_ddl() -> tuple[SchemaMap, Dict[str, Dict[str, Set[str]]]]:
    """Query information_schema for schema/table/columns + index info.

    Returns ``(SchemaMap, IndexMap)``:

    - ``SchemaMap`` is the column registry (today's shape).
    - ``IndexMap`` is ``{schema: {table: {indexed_col, ...}}}`` covering
      PK, FK, and any explicit index columns. Used by
      ``openapi_render_mode=optimized-schema`` (#268) to surface only
      the columns the DB can serve efficiently as concrete simple-filter
      params.

    Harvests every non-system schema the service account has access to
    (DB role grants drive scope). The allowlist YAML (#269) narrows the
    post-harvest cache. Identifiers always bind-bound, never
    interpolated.
    """
    placeholders = ", ".join(["%s"] * len(SYSTEM_SCHEMAS))
    cols_sql = (
        "SELECT table_schema, table_name, column_name "
        "FROM information_schema.columns "
        f"WHERE table_schema NOT IN ({placeholders}) "
        "ORDER BY table_schema, table_name"
    )
    # PK + FK columns from constraints + any other index columns from
    # pg_indexes (partial / multi-column / unique). Joined to schema +
    # table + column names so the result mirrors the column SchemaMap
    # shape. Excludes system schemas.
    index_sql = (
        "SELECT n.nspname AS schema_name, c.relname AS table_name, "
        "       a.attname AS column_name "
        "FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
        f"WHERE n.nspname NOT IN ({placeholders})"
    )
    params = list(SYSTEM_SCHEMAS)
    cols: SchemaMap = {}
    indexes: Dict[str, Dict[str, Set[str]]] = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(cols_sql, params)
            for schema, table, col in await cur.fetchall():
                cols.setdefault(schema, {}).setdefault(table, set()).add(col)
            await cur.execute(index_sql, params)
            for schema, table, col in await cur.fetchall():
                indexes.setdefault(schema, {}).setdefault(table, set()).add(col)
    return cols, indexes
