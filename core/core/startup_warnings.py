"""Surface operationally-hostile defaults at startup.

Some application settings have safe-when-set defaults that are unsafe when
left at their factory zero. ``MAX_PAGE_SIZE=0`` is the headline case:
no cap on ``$count`` means a misbehaving client can request a million
rows and lock up a pool worker. Today's defaults aren't operator-hostile
on every axis, but they are on a few — and the docs already say "set
this in production."

This module emits a single WARNING-level log line per unsafe default at
lifespan startup, after the logger is configured but before the pool
opens. Operators see the warnings in stdout and (when enabled) on Kafka
exactly once per pod start.

The function is shared so adding a new check later doesn't fragment
across the five variants.
"""

import logging

from .engine import schema_cache

logger = logging.getLogger(__name__)


def report(settings) -> None:
    """Emit WARNING-level log lines for unsafe defaults still in effect.

    Called once during each variant's lifespan startup. No-op when every
    check is satisfied — typical in well-configured deployments.
    """
    if settings.max_page_size == 0:
        logger.warning(
            "MAX_PAGE_SIZE=0 — clients can request unbounded result sets. "
            "This is the explicit-opt-out value; the default is 10000."
        )
    if settings.auth.mode == "open":
        logger.warning(
            "AUTH__MODE=open — the service accepts any caller and assumes "
            "no upstream auth. Intended for dev / inside-trust-perimeter "
            "only. Set AUTH__MODE=gateway when running behind a gateway "
            "that validates identity, or AUTH__MODE=jwt to validate "
            "in-process."
        )
    if not settings.auth.require_passthrough:
        logger.warning(
            "AUTH__REQUIRE_PASSTHROUGH=false — data routes accept requests "
            "without X-DB-Authorization, falling back to the configured "
            "service account. Safe only when that account has minimal "
            "grants (e.g., metadata-only `metadata_user`-style)."
        )
    if settings.allowlist_mode == "presentation":
        logger.warning(
            "ALLOWLIST__MODE=presentation — allowlist.yaml is a spec-only "
            "filter (curated docs surface), NOT a security boundary. The "
            "full DDL surface stays reachable at runtime regardless of "
            "what the OpenAPI spec advertises. Use ALLOWLIST__MODE=enforce "
            "(default) when the allowlist needs to gate access."
        )


def report_post_harvest(settings) -> None:
    """Emit INFO-level startup notes that depend on the DDL cache being
    populated.

    Called once after :func:`schema_cache.refresh` and after the allowlist
    has been applied. Surfaces the dynamic-surface size when no allowlist
    is supplied — addresses the footgun where an operator who relied on
    the old ``ALLOWED_SCHEMAS`` env var (removed) silently
    exposes more than they intended after upgrading.
    """
    from .loaders import allowlist

    if allowlist.get().is_empty():
        cache = schema_cache.get_cache()
        schema_count = len(cache)
        table_count = sum(len(tables) for tables in cache.values())
        logger.info(
            "No allowlist.yaml supplied; harvested %d schema(s) / %d table(s) "
            "driven by DB grants. Add allowlist.yaml at the config root with "
            "`schemas:` and/or `tables:` sections to narrow the dynamic surface.",
            schema_count,
            table_count,
        )
