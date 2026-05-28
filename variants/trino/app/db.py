"""Async Trino connection helper, query helpers, and DDL harvest.

Trino uses positional `?` placeholders (paramstyle="qmark") and is strict
about types — unlike PG/MySQL, it won't implicitly compare varchar against
integer columns. Since URL query params arrive as strings, we coerce binds
that look numeric to int/float before binding so `?id=1` works against an
integer column the way it does in every other variant.

## Connection lifecycle

aiotrino has no native pool abstraction, but a single `dbapi.Connection`
can create many `Cursor` instances (one per query) while reusing a single
`aiohttp.ClientSession` underneath for HTTP keep-alive. We keep one
long-lived Connection for the whole process and open a fresh cursor per
fetch_all / harvest_ddl call.

We deliberately do NOT call `Connection.close()` between queries: aiotrino's
`close()` tears down the underlying aiohttp connector, which would kill the
keep-alive benefit. We only close the connection when the variant shuts
down. The shared aiohttp ClientSession is concurrency-safe, so parallel
requests each get their own cursor and proceed without contention.
"""

from typing import Any, Dict, List, Optional, Set, Union

from aiotrino.auth import BasicAuthentication
from aiotrino.dbapi import Connection
from aiotrino.exceptions import DatabaseError as AiotrinoDatabaseError

from core.errors.exceptions import DatabaseError
from core.observability.tracing import try_instrument_trino

from .config import settings

SchemaMap = Dict[str, Dict[str, Set[str]]]

# Trino's catalog/schema model exposes ``information_schema`` per catalog.
# DDL harvest excludes that schema; the connector user's grants determine
# the rest. The allowlist YAML (#269) narrows further when supplied.
SYSTEM_SCHEMAS = ("information_schema",)

_conn: Optional[Connection] = None


def _session_properties() -> dict:
    """Build the Trino session_properties dict, including
    ``query_max_execution_time`` from QUERY_TIMEOUT_SECONDS so a runaway
    query can't hold a coordinator slot. 0 disables. (#17)
    """
    secs = settings.pool.query_timeout_seconds
    return {"query_max_execution_time": f"{secs}s"} if secs > 0 else {}


def _build_connection() -> Connection:
    return Connection(
        host=settings.trino_host,
        port=settings.trino_port,
        user=settings.trino_user,
        catalog=settings.trino_catalog,
        http_scheme=settings.trino_scheme,
        session_properties=_session_properties(),
    )


async def init_pool() -> None:
    """Open the long-lived Trino connection."""
    global _conn
    # aiotrino has no official OpenTelemetry instrumentor; the helper
    # logs the known gap when ENABLE_DB_TRACING=true so operators
    # aren't silently missing Trino-side spans. (#250)
    try_instrument_trino(settings)
    _conn = _build_connection()


async def close_pool() -> None:
    """Close the long-lived Trino connection and its underlying aiohttp session."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def get_pool():
    """Return the shared Trino Connection (kept as `pool` for parity with other variants)."""
    return _conn


def _coerce(v: Any) -> Any:
    """Best-effort coerce string binds to numeric types for Trino's strict typing.

    URL query params always arrive as strings; a column like `id INTEGER` can't
    be compared to varchar in Trino. Try int then float; leave everything else
    as-is. Edge case: a VARCHAR column holding digits ("12345" zipcode) will be
    over-coerced — use $filter with quotes for those.
    """
    if not isinstance(v, str):
        return v
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


async def fetch_all(sql: str, params: Optional[Union[List, Dict]] = None) -> List[Dict]:
    """Execute a SQL query and return all rows as a list of dicts.

    Wraps aiotrino's DatabaseError as core's DatabaseError.
    """
    if _conn is None:
        raise RuntimeError("Trino connection not initialized")
    if isinstance(params, list):
        params = [_coerce(v) for v in params]
    try:
        cur = await _conn.cursor()
        await cur.execute(sql, params or None)
        rows = await cur.fetchall()
        desc = await cur.get_description() or []
        cols = [d[0].lower() for d in desc]
        return [dict(zip(cols, row)) for row in rows]
    except AiotrinoDatabaseError as exc:
        raise DatabaseError(str(exc).strip()) from exc


async def fetch_all_with_creds(
    sql: str,
    params: Optional[Union[List, Dict]],
    creds: tuple[str, str],
) -> List[Dict]:
    """Execute a query using per-request credentials (one-off connection).

    Trino supports HTTP Basic auth; `auth=BasicAuthentication(user, pass)` is
    the aiotrino-sanctioned way to pass credentials. Basic auth requires
    `http_scheme=https` — Trino rejects Basic over plain HTTP.
    """
    if isinstance(params, list):
        params = [_coerce(v) for v in params]
    conn = Connection(
        host=settings.trino_host,
        port=settings.trino_port,
        user=creds[0],
        catalog=settings.trino_catalog,
        http_scheme=settings.trino_scheme,
        auth=BasicAuthentication(creds[0], creds[1]),
        session_properties=_session_properties(),
    )
    try:
        cur = await conn.cursor()
        await cur.execute(sql, params or None)
        rows = await cur.fetchall()
        desc = await cur.get_description() or []
        cols = [d[0].lower() for d in desc]
        return [dict(zip(cols, row)) for row in rows]
    except AiotrinoDatabaseError as exc:
        raise DatabaseError(str(exc).strip()) from exc
    finally:
        await conn.close()


async def ping() -> None:
    """Readiness check — runs SELECT 1 through the shared long-lived Connection."""
    if _conn is None:
        raise RuntimeError("Trino connection not initialized")
    cur = await _conn.cursor()
    await cur.execute("SELECT 1")
    await cur.fetchall()


async def get_connection_limit() -> tuple[Optional[int], str]:
    """Trino has no per-coordinator connection-limit knob equivalent to
    Postgres ``max_connections`` or MySQL ``@@max_connections``. The pool-
    sizing report skips the math for Trino. (#63)
    """
    return None, "Trino has no pool concept — see docs/reference/variants.md"


async def harvest_ddl() -> tuple[SchemaMap, Dict[str, Dict[str, Set[str]]]]:
    """Query information_schema in the configured catalog for
    schema/table/columns. Returns an empty IndexMap.

    Trino's connector ecosystem doesn't uniformly expose index info
    through ``information_schema`` — backends vary, columnar engines
    treat indexes as partition / sort keys rather than secondary
    indexes, and many connectors don't surface anything index-shaped
    at all. Index harvest is therefore best-effort and currently
    returns empty; ``openapi_render_mode=optimized-schema`` degrades
    to simple-schema layout for Trino-fronted deployments. (#268)

    Harvests every non-system schema the connector user has access to.
    The allowlist YAML (#269) narrows the cache post-harvest.
    Identifiers always bind-bound.
    """
    if _conn is None:
        raise RuntimeError("Trino connection not initialized")
    placeholders = ", ".join(["?"] * len(SYSTEM_SCHEMAS))
    sql = (
        "SELECT table_schema, table_name, column_name "
        "FROM information_schema.columns "
        f"WHERE table_schema NOT IN ({placeholders}) "
        "ORDER BY table_schema, table_name"
    )
    params = list(SYSTEM_SCHEMAS)
    cols: SchemaMap = {}
    cur = await _conn.cursor()
    await cur.execute(sql, params)
    for schema, table, col in await cur.fetchall():
        cols.setdefault(schema, {}).setdefault(table, set()).add(col)
    return cols, {}
