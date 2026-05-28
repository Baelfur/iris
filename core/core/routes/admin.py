"""Admin endpoints — gated by the admin dispatcher, not the user-facing JWT.

Endpoints (all under the admin sub-app, mounted at ``/admin``):

- ``POST /admin/refresh-schema`` — re-harvest DDL into the cache without
  restarting the pod.
- ``GET /admin/pool-sizing`` — same pool-sizing report that's logged at
  startup, as JSON for runbooks and dashboards.
- ``POST /admin/reload-config`` — refresh the validation/ and queries/
  YAMLs from the configured source. For ``git`` source this pulls
  the configured branch; for ``local`` it just re-reads disk. Re-runs
  both loaders.
- ``GET /admin/catalog`` — JSON dump of harvested DDL + view defs +
  custom queries, for tooling that wants the raw catalog (audit
  scripts, doc generators, capacity planning). Devs / dev-facing
  tooling don't need this; the dev OpenAPI spec is per-deployment-
  concrete.

The dispatcher (``verify_admin_access``) accepts either
``X-Admin-Token`` (shared static secret, today's default) or a Bearer
JWT carrying the configured admin claim. Both
paths fail closed when their respective settings are unset. The
router has no ``prefix`` because the admin sub-app is mounted at
``/admin`` in ``app_meta.build_app`` — adding a prefix here would
double the segment.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import openapi_dynamic
from ..auth.admin import verify_admin_access
from ..catalog import iter_queries, iter_tables
from ..context import get_context
from ..engine import pool_sizing, schema_cache
from ..loaders import allowlist
from ..loaders import queries as custom_queries
from ..loaders import validation as view_defs

router = APIRouter(tags=["admin"])
logger = logging.getLogger(__name__)


def _invalidate_dev_openapi(request: Request) -> None:
    """Clear the dev app's cached OpenAPI spec so the next /openapi.json
    request regenerates with the updated catalog. The admin sub-app
    keeps a reference to the parent (dev) app on ``state.parent_app``;
    no-op when the reference is missing (e.g., admin router used
    directly in tests without the sub-app mount)."""
    parent_app = getattr(request.app.state, "parent_app", None)
    if parent_app is not None:
        openapi_dynamic.invalidate(parent_app)


@router.post("/refresh-schema")
async def refresh_schema(request: Request):
    """Re-harvest DDL metadata without restarting."""
    await verify_admin_access(request)
    count = await schema_cache.refresh()
    _invalidate_dev_openapi(request)
    logger.info("Schema refresh: %d tables cached", count)
    return {"message": f"Refreshed: {count} tables cached"}


@router.get("/pool-sizing")
async def pool_sizing_report(request: Request):
    """Return the same pool-sizing report logged at startup, as JSON."""
    await verify_admin_access(request)
    ctx = get_context()
    if ctx.get_connection_limit is None:
        db_max, src = None, f"{ctx.database} (probe not implemented)"
    else:
        db_max, src = await ctx.get_connection_limit()
    report = pool_sizing.compute_report(
        db_max=db_max,
        db_max_source=src,
        pool_min=ctx.settings.pool.min_size,
        pool_max=ctx.settings.pool.max_size,
        hpa_max=ctx.settings.pool.hpa_max_replicas,
    )
    # Identify which deployment this report belongs to — relevant when
    # one runbook hits multiple instances.
    if ctx.settings.deployment_name:
        report["deployment"] = ctx.settings.deployment_name
    return report


@router.get("/catalog")
async def catalog(request: Request) -> dict[str, Any]:
    """Return the harvested DDL + view defs + custom queries as JSON.

    Operator-facing — admin-token gated for the same reason ``GET /queries``
    is gated: the catalog is pre-attack reconnaissance surface, and
    end-users get the URLs they're meant to call out-of-band rather than
    enumerating. Tooling that programmatically inspects a deployment
    (audit scripts, doc generators, capacity-planning reports) hits this
    endpoint instead of reading the per-table OpenAPI operations one by
    one.

    Shape::

        {
          "schemas": {
            "<schema>": {
              "<table>": {
                "columns": ["id", "name", ...],
                "view_def": {"required": [...], "optional": [...]} | null
              }
            }
          },
          "queries": {
            "<path>": {
              "name": "<name>",
              "required": [...],
              "optional": [...]
            }
          },
          "deployment": "<deployment_name>"  // when set
        }
    """
    await verify_admin_access(request)
    ctx = get_context()

    schemas: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in iter_tables():
        schemas.setdefault(entry.schema, {})[entry.table] = {
            "columns": sorted(entry.columns),
            "view_def": (
                {
                    "required": sorted(entry.view_def.required),
                    "optional": sorted(entry.view_def.optional),
                }
                if entry.view_def is not None
                else None
            ),
        }

    queries: dict[str, dict[str, Any]] = {}
    for q in iter_queries():
        queries[q.path] = {
            "name": q.qdef.name,
            "required": sorted(q.qdef.view_def.required),
            "optional": sorted(q.qdef.view_def.optional),
        }

    payload: dict[str, Any] = {"schemas": schemas, "queries": queries}
    if ctx.settings.deployment_name:
        payload["deployment"] = ctx.settings.deployment_name
    return payload


@router.post("/reload-config")
async def reload_config(request: Request):
    """Refresh validation/ and queries/ YAMLs from the configured source.

    For ``CONFIG__SOURCE=git``, this pulls the configured branch.
    For ``CONFIG__SOURCE=local``, this just re-reads disk (useful when
    operators bind-mount config into the pod and update files in place).

    Returns counts of each kind loaded so the caller can confirm what
    landed without grepping logs.
    """
    await verify_admin_access(request)
    ctx = get_context()
    if ctx.config_source is None:
        # Variants always wire this in lifespan; this branch protects
        # against a future refactor that forgets to. Surface as 500
        # because the caller hasn't done anything wrong — it's a
        # server-side wiring bug that needs operator attention.
        raise HTTPException(500, "config source not initialized")

    cfg_root = ctx.config_source.reload()
    vcount = view_defs.load_views(str(cfg_root / "validation"))
    qcount = custom_queries.load_queries(str(cfg_root / "queries"))
    if vcount:
        view_defs.warn_mismatches(schema_cache.get_cache())
    # Re-apply allowlist against the existing cache. Note: we don't
    # re-harvest here — operators wanting the cache rebuilt run
    # /admin/refresh-schema. This endpoint is for YAML-only changes.
    # In presentation mode the cache is left alone — the spec
    # renderer applies the allowlist on the next /openapi.json request.
    allowlist.load(str(cfg_root))
    allowlist.narrow_cache(schema_cache.get_cache(), mode=ctx.settings.allowlist_mode)
    _invalidate_dev_openapi(request)
    logger.info(
        "Config reloaded from %s: %d view def(s), %d custom query/queries",
        ctx.settings.config.source,
        vcount,
        qcount,
    )
    payload: dict[str, Any] = {"view_defs": vcount, "queries": qcount}
    if ctx.settings.deployment_name:
        payload["deployment"] = ctx.settings.deployment_name
    return payload
