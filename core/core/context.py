"""Application context — variants inject database-specific dependencies here.

Each variant's main.py builds an AppContext during startup and calls set_context().
Shared routes and helpers read from get_context() at request time.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Literal,
    Optional,
)

if TYPE_CHECKING:
    # Type-only imports keep ``context.py`` importable from anywhere
    # in core without dragging the engine/config subpackages into
    # the import graph at runtime.
    from .config.settings import AppSettings
    from .config.source import ConfigSource
    from .engine.circuit_breaker import CircuitBreaker

FetchAll = Callable[[str, dict | list | None], Awaitable[list[dict]]]
FetchAllWithCreds = Callable[[str, dict | list | None, tuple[str, str]], Awaitable[list[dict]]]
HarvestDDL = Callable[[], Awaitable[dict[str, dict[str, set]]]]
Ping = Callable[[], Awaitable[None]]
# (limit, source_label). limit is None when the probe couldn't read the
# server's connection ceiling (Trino has no concept; Oracle metadata
# accounts often lack v$parameter SELECT). The source label always carries
# why — "Postgres pg_settings.max_connections", "Oracle v$parameter read
# denied (DatabaseError)", etc. Used by core.pool_sizing.
GetConnectionLimit = Callable[[], Awaitable[tuple[int | None, str]]]


@dataclass
class AppContext:
    """Runtime dependencies wired by each variant."""

    fetch_all: FetchAll
    harvest_ddl: HarvestDDL
    # Placeholder dialect: "pyformat" (%(name)s) for postgres/mysql/mariadb,
    # "named" (:name) for oracle, "qmark" (?) for trino.
    paramstyle: Literal["pyformat", "named", "qmark"]
    # Variants subclass AppSettings to add their connection vars
    # (pg_user, mysql_host, etc.) — the shared fields the service reads at
    # runtime are all defined on the base class.
    settings: "AppSettings"
    database: str  # "postgresql" / "mysql" / "mariadb" / "oracle" / "trino"
    fetch_all_with_creds: FetchAllWithCreds | None = field(default=None)
    ping: Ping | None = field(default=None)
    get_connection_limit: GetConnectionLimit | None = field(default=None)
    # Optional circuit breaker — populated by lifespan when
    # CIRCUIT_BREAKER_ENABLED=true. Routes call core.circuit_breaker
    # .fetch_with_breaker(ctx, ...) which is a no-op pass-through when
    # this is None.
    breaker: Optional["CircuitBreaker"] = field(default=None)
    # ConfigSource that materialized validation/ and queries/. /admin/
    # reload-config calls .reload() on this to refresh from the source
    # (git pull, etc.) before re-running the loaders.
    config_source: Optional["ConfigSource"] = field(default=None)
    # Configured service-account username, redacted from driver-error
    # text in routes before logging or shipping to Kafka. Each variant
    # wires this from its DB-specific user setting (pg_user, mysql_user,
    # etc.). Empty string is allowed for the no-op case but every
    # production variant sets it.
    db_user: str = ""


_ctx: AppContext | None = None


def set_context(ctx: AppContext) -> None:
    """Install the variant-supplied AppContext as the module singleton.

    Called once per process from each variant's lifespan handler after
    the connection pool is up. Subsequent calls overwrite (used by
    tests injecting a fake context); production code only calls this
    during startup.
    """
    global _ctx
    _ctx = ctx


def get_context() -> AppContext:
    """Return the installed AppContext or raise if startup hasn't run yet.

    Shared route handlers and helpers call this at request time to reach
    the variant's ``fetch_all`` / ``harvest_ddl`` / ``ping`` / settings
    without importing the variant package directly. Raising on missing
    context surfaces a startup-order bug as a clear RuntimeError instead
    of an attribute access on ``None``.
    """
    if _ctx is None:
        raise RuntimeError(
            "Application context not initialized — call set_context() during startup"
        )
    return _ctx
