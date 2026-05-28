"""Async MariaDB connection pool, query helpers, and DDL harvest."""

from typing import Dict, List, Optional, Set

import aiomysql
import pymysql

from core.errors.exceptions import DatabaseError
from core.observability.tracing import try_instrument_aiomysql

from .config import settings

SchemaMap = Dict[str, Dict[str, Set[str]]]

# MariaDB built-in admin/metadata schemas excluded from DDL harvest.
# Anything outside this set that the service account has SELECT on
# becomes part of the dynamic surface. The allowlist YAML (#269)
# narrows further when supplied.
SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")

pool: Optional[aiomysql.Pool] = None


def _query_timeout_init_command() -> Optional[str]:
    """SET SESSION max_statement_time (seconds; decimal allowed) — applied
    per connection so a runaway SELECT can't hold a pool worker. Returns
    None when the timeout is disabled (0). MariaDB diverges from MySQL
    here: name is ``max_statement_time`` and the unit is seconds, not
    milliseconds. (#17)
    """
    secs = settings.pool.query_timeout_seconds
    return f"SET SESSION max_statement_time = {secs}" if secs > 0 else None


async def init_pool() -> None:
    """Create the async MariaDB connection pool."""
    global pool
    # OpenTelemetry aiomysql instrumentation (MariaDB shares the
    # driver). Opt-in via ENABLE_DB_TRACING. (#250)
    try_instrument_aiomysql(settings)
    init_command = _query_timeout_init_command()
    kwargs = {
        "host": settings.mariadb_host,
        "port": settings.mariadb_port,
        "user": settings.mariadb_user,
        "password": settings.mariadb_password,
        "db": settings.mariadb_database,
        "minsize": settings.pool.min_size,
        "maxsize": settings.pool.max_size,
    }
    if init_command:
        kwargs["init_command"] = init_command
    pool = await aiomysql.create_pool(**kwargs)


async def close_pool() -> None:
    """Close the connection pool and release resources."""
    global pool
    if pool:
        pool.close()
        await pool.wait_closed()
        pool = None


def get_pool():
    """Return the current connection pool."""
    return pool


async def fetch_all(sql: str, params: Optional[Dict] = None) -> List[Dict]:
    """Execute a SQL query and return all rows as a list of dicts.

    Wraps pymysql.DatabaseError as DatabaseError.
    """
    if not pool:
        raise RuntimeError("Connection pool not initialized")
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params or {})
                cols = [c[0].lower() for c in cur.description]
                return [dict(zip(cols, row)) for row in await cur.fetchall()]
    except pymysql.DatabaseError as exc:
        raise DatabaseError(str(exc).strip()) from exc


async def fetch_all_with_creds(
    sql: str,
    params: Optional[Dict],
    creds: tuple[str, str],
) -> List[Dict]:
    """Execute a query using per-request credentials (one-off connection)."""
    init_command = _query_timeout_init_command()
    kwargs = {
        "host": settings.mariadb_host,
        "port": settings.mariadb_port,
        "user": creds[0],
        "password": creds[1],
        "db": settings.mariadb_database,
    }
    if init_command:
        kwargs["init_command"] = init_command
    try:
        conn = await aiomysql.connect(**kwargs)
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql, params or {})
                cols = [c[0].lower() for c in cur.description]
                return [dict(zip(cols, row)) for row in await cur.fetchall()]
        finally:
            conn.close()
    except pymysql.DatabaseError as exc:
        raise DatabaseError(str(exc).strip()) from exc


async def ping() -> None:
    """Readiness check — acquire a pool connection and run SELECT 1."""
    if not pool:
        raise RuntimeError("Connection pool not initialized")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()


async def get_connection_limit() -> tuple[Optional[int], str]:
    """Read the server's @@max_connections. (#63)"""
    if not pool:
        raise RuntimeError("Connection pool not initialized")
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT @@max_connections")
                row = await cur.fetchone()
                if row and row[0] is not None:
                    return int(row[0]), "MariaDB @@max_connections"
                return None, "MariaDB @@max_connections (no row)"
    except pymysql.DatabaseError as exc:
        return None, f"MariaDB @@max_connections read failed: {type(exc).__name__}"


async def harvest_ddl() -> tuple[SchemaMap, Dict[str, Dict[str, Set[str]]]]:
    """Query information_schema for schema/table/columns + indexed cols.

    Returns ``(SchemaMap, IndexMap)``. Same shape as the MySQL variant —
    MariaDB's STATISTICS view covers PK, FK (when indexed), and explicit
    user indexes.

    Harvests every non-system schema the service account has access to.
    The allowlist YAML (#269) narrows the cache post-harvest.
    Identifiers always bind-bound.
    """
    placeholders = ", ".join(["%s"] * len(SYSTEM_SCHEMAS))
    cols_sql = (
        "SELECT table_schema, table_name, column_name "
        "FROM information_schema.columns "
        f"WHERE table_schema NOT IN ({placeholders}) "
        "ORDER BY table_schema, table_name"
    )
    index_sql = (
        "SELECT table_schema, table_name, column_name "
        "FROM information_schema.statistics "
        f"WHERE table_schema NOT IN ({placeholders})"
    )
    params = list(SYSTEM_SCHEMAS)
    cols: SchemaMap = {}
    indexes: Dict[str, Dict[str, Set[str]]] = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(cols_sql, params)
            for schema, table, col in await cur.fetchall():
                cols.setdefault(schema, {}).setdefault(table, set()).add(col)
            await cur.execute(index_sql, params)
            for schema, table, col in await cur.fetchall():
                indexes.setdefault(schema, {}).setdefault(table, set()).add(col)
    return cols, indexes
