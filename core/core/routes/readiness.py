"""Readiness probe — /ready and /readyz.

Separate from /health (process-alive only). Readiness pings the configured
database via the variant's ctx.ping() to confirm the pod can actually serve
traffic. Used by Kubernetes readinessProbe so pods with a broken DB
connection drain from the service's endpoint list instead of returning 5xx.

Response:
    200  {"status": "ready"}    pod is serving traffic
    503  {"status": "not ready", "reason": "..."}    don't send traffic here

Behavior toggles:
    READINESS_TIMEOUT_MS=500   default — 500ms cap on the ping
    READINESS_TIMEOUT_MS=0     disabled — endpoint still returns 200 but
                               skips the DB hit (equivalent to /health)

Two paths (/ready, /readyz) for cross-convention compatibility with existing
k8s manifests.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from ..context import get_context

router = APIRouter(tags=["readiness"])
logger = logging.getLogger(__name__)


def _ready_payload(reason: str = "") -> dict:
    """Build the response body. Includes ``deployment`` when set so ops
    dashboards hitting probes can identify the specific instance."""
    payload: dict = {"status": "not ready", "reason": reason} if reason else {"status": "ready"}
    deployment = get_context().settings.deployment_name
    if deployment:
        payload["deployment"] = deployment
    return payload


async def _probe():
    """Run the configured readiness check. Raises HTTPException(503) on failure."""
    ctx = get_context()
    timeout_ms = max(0, ctx.settings.readiness_timeout_ms)

    # Disabled — process-alive only.
    if timeout_ms == 0 or ctx.ping is None:
        return _ready_payload()

    try:
        await asyncio.wait_for(ctx.ping(), timeout=timeout_ms / 1000)
        return _ready_payload()
    except TimeoutError as exc:
        logger.warning("Readiness probe timed out after %dms", timeout_ms)
        raise HTTPException(503, _ready_payload(reason="db ping timed out")) from exc
    except Exception as exc:
        logger.warning("Readiness probe failed: %s", type(exc).__name__)
        raise HTTPException(503, _ready_payload(reason="db unreachable")) from exc


@router.get("/ready")
async def ready():
    """Database readiness probe. Pings the DB via ``ctx.ping()`` and returns
    200 when reachable, 503 when not. Honors ``READINESS_TIMEOUT_MS`` to cap
    the ping (set to 0 to skip the DB hit and behave like /health). Body
    carries ``deployment`` when ``DEPLOYMENT_NAME`` is set.

    Typical consumer is a Kubernetes readinessProbe — pods with a broken
    DB connection drain from the service's endpoint list — but the endpoint
    is environment-agnostic.
    """
    return await _probe()


@router.get("/readyz")
async def readyz():
    """Alias for :func:`ready` — same behavior, second URL for the ``-z``
    suffix convention some readiness-probe configs prefer.
    """
    return await _probe()
