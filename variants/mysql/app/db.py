"""Async MySQL connection pool, query helpers, and DDL harvest."""

from typing import Dict, List, Optional, Set

import aiomysql
import pymysql

from core.errors.exceptions import DatabaseError
from core.observability.tracing import try_instrument_aiomysql

from .config import settings

SchemaMap = Dict[str, Dict[str, Set[str]]]

# MySQL ships these as built-in admin/metadata schemas. Empty
# Built-in admin/metadata schemas excluded from DDL harvest. Anything
# outside this set that the service account has SELECT on becomes part
# of the dynamic surface. The allowlist YAML (#269) narrows further.
# outside this set. (#64)
SYSTEM_SCHEMAS = ("mysql", "information_schema", "performance_schema", "sys")

pool: Optional[aiomysql.Pool] = None


def _query_timeout_init_command() -> Optional[str]:
    """SET SESSION MAX_EXECUTION_TIME (milliseconds) — applied per
    connection so a runaway SELECT can't hold a pool worker. Returns
    None when the timeout is disabled (0). MySQL only enforces this for
    SELECT; that matches the service's read-only surface. (#17)
    """
    timeout_ms = settings.pool.query_timeout_seconds * 1000
    return f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}" if timeout_ms > 0 else None


async def init_pool() -> None:
    """Create the async MySQL connection pool."""
    global pool
    # OpenTelemetry aiomysql instrumentation, opt-in via
    # ENABLE_DB_TRACING. No-op when the gate is closed. (#250)
    try_instrument_aiomysql(settings)
    init_command = _query_timeout_init_command()
    kwargs = {
        "host": settings.mysql_host,
        "port": settings.mysql_port,
        "user": settings.mysql_user,
        "password": settings.mysql_password,
        "db": settings.mysql_database,
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
        "host": settings.mysql_host,
        "port": settings.mysql_port,
        "user": creds[0],
        "password": creds[1],
        "db": settings.mysql_database,
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
                    return int(row[0]), "MySQL @@max_connections"
                return None, "MySQL @@max_connections (no row)"
    except pymysql.DatabaseError as exc:
        return None, f"MySQL @@max_connections read failed: {type(exc).__name__}"


async def harvest_ddl() -> tuple[SchemaMap, Dict[str, Dict[str, Set[str]]]]:
    """Query information_schema for schema/table/columns + indexed cols.

    Returns ``(SchemaMap, IndexMap)``. Index info comes from
    ``information_schema.STATISTICS`` which covers PK, FK (when an
    index backs them), and any explicit user indexes.

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
