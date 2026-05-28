"""Async Oracle connection pool, query helpers, and DDL harvest."""

from typing import Dict, List, Optional, Set

import oracledb

from core.errors.exceptions import DatabaseError
from core.observability.tracing import try_instrument_oracledb

from .config import settings

SchemaMap = Dict[str, Dict[str, Set[str]]]

# Oracle ships dozens of system/admin schemas. DDL harvest excludes
# this set; the service account's SELECT grants determine the rest.
# The allowlist YAML (#269) narrows further when supplied. The list
# covers the standard install plus optional components most enterprise
# installs leave present.
SYSTEM_SCHEMAS = (
    "SYS",
    "SYSTEM",
    "XDB",
    "OUTLN",
    "MDSYS",
    "CTXSYS",
    "OLAPSYS",
    "WMSYS",
    "EXFSYS",
    "DBSNMP",
    "APPQOSSYS",
    "AUDSYS",
    "GSMADMIN_INTERNAL",
    "LBACSYS",
    "ORDDATA",
    "ORDPLUGINS",
    "ORDSYS",
    "SI_INFORMTN_SCHEMA",
    "DVSYS",
    "OJVMSYS",
    "ANONYMOUS",
    "XS$NULL",
    "DIP",
    "GGSYS",
    "REMOTE_SCHEDULER_AGENT",
)

pool: Optional[oracledb.AsyncConnectionPool] = None


async def init_pool() -> None:
    """Create the async Oracle connection pool."""
    global pool
    # OpenTelemetry oracledb instrumentation, opt-in via
    # ENABLE_DB_TRACING. Wrapped in best-effort import + try/except
    # inside try_instrument_oracledb because async-mode oracledb
    # instrumentation has had rough edges in past releases. (#250)
    try_instrument_oracledb(settings)
    pool = oracledb.create_pool_async(
        user=settings.oracle_user,
        password=settings.oracle_password,
        dsn=settings.dsn,
        min=settings.pool.min_size,
        max=settings.pool.max_size,
    )


async def close_pool() -> None:
    """Close the connection pool and release resources."""
    global pool
    if pool:
        await pool.close()
        pool = None


def get_pool():
    """Return the current connection pool."""
    return pool


def _apply_call_timeout(conn) -> None:
    """Set per-call timeout (milliseconds) so a runaway query can't hold a
    pool worker. Property assignment — no DB roundtrip. 0 disables. (#17)
    """
    timeout_ms = settings.pool.query_timeout_seconds * 1000
    if timeout_ms > 0:
        conn.call_timeout = timeout_ms


async def fetch_all(sql: str, params: Optional[Dict] = None) -> List[Dict]:
    """Execute a SQL query and return all rows as a list of dicts.

    Wraps oracledb.DatabaseError as DatabaseError.
    """
    if not pool:
        raise RuntimeError("Connection pool not initialized")
    try:
        async with pool.acquire() as conn:
            _apply_call_timeout(conn)
            async with conn.cursor() as cur:
                await cur.execute(sql, params or {})
                cols = [c[0].lower() for c in cur.description]
                return [dict(zip(cols, row)) async for row in cur]
    except oracledb.DatabaseError as exc:
        error = exc.args[0]
        raise DatabaseError(str(error.message).strip()) from exc


async def fetch_all_with_creds(
    sql: str,
    params: Optional[Dict],
    creds: tuple[str, str],
) -> List[Dict]:
    """Execute a query using per-request credentials (one-off connection)."""
    try:
        async with oracledb.connect_async(
            user=creds[0],
            password=creds[1],
            dsn=settings.dsn,
        ) as conn:
            _apply_call_timeout(conn)
            async with conn.cursor() as cur:
                await cur.execute(sql, params or {})
                cols = [c[0].lower() for c in cur.description]
                return [dict(zip(cols, row)) async for row in cur]
    except oracledb.DatabaseError as exc:
        error = exc.args[0]
        raise DatabaseError(str(error.message).strip()) from exc


async def ping() -> None:
    """Readiness check — Oracle requires a FROM clause, so SELECT 1 FROM DUAL."""
    if not pool:
        raise RuntimeError("Connection pool not initialized")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM DUAL")
            await cur.fetchone()


async def get_connection_limit() -> tuple[Optional[int], str]:
    """Read the server's processes limit from v$parameter. (#63)

    Metadata-only Oracle service accounts often don't have SELECT on
    ``v$parameter`` — that's expected. Catch the privilege error and
    return the reason in the source label so the report says
    "permission denied" rather than failing startup.
    """
    if not pool:
        raise RuntimeError("Connection pool not initialized")
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT TO_NUMBER(value) FROM v$parameter WHERE name = 'processes'"
                )
                row = await cur.fetchone()
                if row and row[0] is not None:
                    return int(row[0]), "Oracle v$parameter.processes"
                return None, "Oracle v$parameter.processes (no row)"
    except oracledb.DatabaseError as exc:
        # ORA-00942 (table or view does not exist) typically means the
        # service account lacks SELECT on v$parameter — fail soft.
        return None, f"Oracle v$parameter read denied ({type(exc).__name__})"


async def harvest_ddl() -> tuple[SchemaMap, Dict[str, Dict[str, Set[str]]]]:
    """Query ALL_TAB_COLUMNS for columns + ALL_IND_COLUMNS for indexes.

    Returns ``(SchemaMap, IndexMap)``. Index info covers PK, FK (when
    indexed), and explicit user indexes via ``ALL_IND_COLUMNS``.

    Harvests every non-system schema the service account has SELECT on.
    The allowlist YAML (#269) narrows the cache post-harvest.
    Cache keys are lowercase for case-insensitive lookups; SQL uses
    uppercase Oracle owners. Identifiers always bind-bound.
    """
    placeholders = ", ".join(f":s{i}" for i in range(len(SYSTEM_SCHEMAS)))
    binds = {f"s{i}": v for i, v in enumerate(SYSTEM_SCHEMAS)}
    cols_sql = (
        "SELECT owner, table_name, column_name "
        f"FROM all_tab_columns WHERE owner NOT IN ({placeholders}) "
        "ORDER BY owner, table_name"
    )
    index_sql = (
        "SELECT table_owner, table_name, column_name "
        f"FROM all_ind_columns WHERE table_owner NOT IN ({placeholders})"
    )
    cols: SchemaMap = {}
    indexes: Dict[str, Dict[str, Set[str]]] = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(cols_sql, binds)
            async for owner, table, col in cur:
                cols.setdefault(owner.lower(), {}).setdefault(table.lower(), set()).add(
                    col.lower()
                )
            await cur.execute(index_sql, binds)
            async for owner, table, col in cur:
                indexes.setdefault(owner.lower(), {}).setdefault(
                    table.lower(), set()
                ).add(col.lower())
    return cols, indexes
