"""Pool-sizing recommendation report.

Audience: operators who haven't sized a connection pool before. Produces a
concrete log line at startup ("here's your DB's capacity, here's your pool
config, here's the math at peak HPA") plus a JSON shape for the
``GET /admin/pool-sizing`` endpoint. Advice only — never auto-applied.

Design principles :

- Never auto-apply. Other DB consumers (BI, crons, sibling services) make
  the service's view incomplete. Recommendations help operators decide; they don't
  enforce.
- Fall back cleanly. Variants that can't read the limit (Trino has no
  concept; Oracle metadata accounts often lack ``v$parameter`` SELECT)
  return ``None`` with a source label explaining why. Report says
  "unknown" rather than failing startup.
- No runtime cost. Probe runs once at startup; admin endpoint reuses the
  cached result if you build it that way (today's implementation re-probes
  on demand, which is fine for an operator-facing endpoint).
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _verdict(used_pct: float) -> str:
    """Class label for a given peak-utilization percentage.

    Bands chosen for readability in a startup log line — not a precise
    threshold model. "ok" up to 70% leaves real headroom for other
    consumers; "caution" up to 90% means it works but isn't comfortable;
    "unsafe" past that means the next other-consumer spike trips it.
    """
    if used_pct <= 70:
        return "ok"
    if used_pct <= 90:
        return "caution"
    return "unsafe"


def _recommendations(
    db_max: int,
    hpa_max: int,
    current_pool_max: int,
) -> list[dict[str, Any]]:
    """Return at-most-three example pool-max sizes with their utilization.

    Always includes the currently-configured ``pool_max`` so operators see
    "where you are" alongside neighboring options. The two adjacent samples
    bracket it for context.
    """
    if hpa_max <= 0 or db_max <= 0:
        return []
    samples = sorted(
        {
            max(1, current_pool_max - 2),
            current_pool_max,
            current_pool_max + 2,
            current_pool_max + 5,
        }
    )
    out = []
    for sample in samples:
        used_pct = round(sample * hpa_max * 100 / db_max)
        out.append(
            {
                "pool_max": sample,
                "used_pct": used_pct,
                "verdict": _verdict(used_pct),
            }
        )
    return out


def compute_report(
    db_max: int | None,
    db_max_source: str,
    pool_min: int,
    pool_max: int,
    hpa_max: int,
) -> dict[str, Any]:
    """Build the pool-sizing report payload.

    Pure function — no I/O, no logging. Same shape used for both the
    startup log line (rendered via :func:`format_report_for_log`) and the
    ``GET /admin/pool-sizing`` JSON response.
    """
    total_at_peak = pool_max * hpa_max
    capacity_used_pct: int | None = None
    if db_max and db_max > 0:
        capacity_used_pct = round(total_at_peak * 100 / db_max)
    return {
        "db_max_connections": db_max,
        "db_max_connections_source": db_max_source,
        "pool": {"min": pool_min, "max": pool_max},
        "expected_replica_peak": hpa_max,
        "total_at_peak": total_at_peak,
        "capacity_used_pct": capacity_used_pct,
        "recommendations": _recommendations(db_max, hpa_max, pool_max) if db_max else [],
    }


def format_report_for_log(report: dict[str, Any]) -> str:
    """Render the report dict as a multi-line, network-engineer-friendly
    block suitable for a single ``logger.info(...)`` call. Explicit math
    over abstract advice — the audience for this is operators who'd rather
    see ``10 pods × 10 conns = 100`` than ``use 10``.
    """
    lines = ["Pool sizing report"]
    db_max = report["db_max_connections"]
    src = report["db_max_connections_source"]
    if db_max:
        lines.append(f"  DB max_connections:    {db_max}    ({src})")
    else:
        lines.append(f"  DB max_connections:    unknown    ({src})")
    pool = report["pool"]
    lines.append(f"  Pool configured:       min={pool['min']}, max={pool['max']}")
    lines.append(
        f"  Expected replica peak: {report['expected_replica_peak']}    (HPA_MAX_REPLICAS)"
    )
    if db_max:
        used = report["capacity_used_pct"]
        lines.append(
            f"  Total at peak:         {report['total_at_peak']} connections    "
            f"({used}% of DB capacity)"
        )
        lines.append("  Recommendations:")
        for rec in report["recommendations"]:
            marker = " ← current" if rec["pool_max"] == pool["max"] else ""
            lines.append(
                f"    pool_max={rec['pool_max']:<3}  uses {rec['used_pct']:>3}%  ({rec['verdict']}){marker}"
            )
    else:
        lines.append(f"  Total at peak:         {report['total_at_peak']} connections")
        lines.append("  Recommendations:       cannot advise without observed DB limit")
    return "\n".join(lines)


async def report_pool_sizing(ctx) -> None:
    """Probe the variant for its connection limit and log the report.

    Called once during lifespan startup, after the DDL harvest. Never
    raises — a failure in the report path must not prevent the pod from
    becoming ready.
    """
    try:
        if ctx.get_connection_limit is None:
            db_max, src = None, f"{ctx.database} (probe not implemented)"
        else:
            db_max, src = await ctx.get_connection_limit()
        report = compute_report(
            db_max=db_max,
            db_max_source=src,
            pool_min=ctx.settings.pool.min_size,
            pool_max=ctx.settings.pool.max_size,
            hpa_max=ctx.settings.pool.hpa_max_replicas,
        )
        logger.info(format_report_for_log(report))
    except Exception as exc:
        logger.warning(
            "Pool-sizing report skipped: %s: %s",
            type(exc).__name__,
            exc,
        )
