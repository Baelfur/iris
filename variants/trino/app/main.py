"""FastAPI application entrypoint for the Trino variant.

Trino has a 3-segment route ``/{catalog}/{schema}/{view_name}`` that no
other variant has, so this file passes an ``extra_routes`` callback to
``build_app`` which registers the catalog endpoint after the standard
routers and before the error handler. (#152, #197)
"""

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request

from core.app_meta import build_app
from core.engine.query_params import ClosedGrammarParams
from core.errors.messages import error_msg
from core.handlers.inventory import query_view_impl

from . import db
from .config import settings


def _register_catalog_route(app: FastAPI, settings) -> None:
    """Trino-only 3-segment route ``/{catalog}/{schema}/{view_name}``.

    Lets callers fully qualify the table reference as
    ``catalog.schema.table`` — matching Trino's native FQN form and the
    legacy ``flaskdsl-trino`` URL shape that consumers migrated from. The
    catalog must match the connection's configured catalog (DDL is
    harvested only against that one); cross-catalog querying would
    require harvesting ``information_schema`` across all reachable
    catalogs and is out of scope for this surface. (#152)
    """

    @app.get("/{catalog}/{schema}/{view_name}")
    async def query_view_with_catalog(
        request: Request,
        catalog: str,
        schema: str,
        view_name: str,
        params: Annotated[ClosedGrammarParams, Depends()],
    ):
        if catalog.lower() != settings.trino_catalog.lower():
            raise HTTPException(
                404, error_msg(f"Catalog '{catalog}' not configured", "Not found")
            )
        return await query_view_impl(
            request,
            schema,
            view_name,
            params.select,
            params.filter_,
            params.orderby,
            params.count,
            params.start_index,
            params.groupby,
            params.having,
            catalog=catalog,
            cursor=params.cursor,
        )


app = build_app(
    database="trino",
    paramstyle="qmark",
    settings=settings,
    db_module=db,
    db_user=settings.trino_user,
    extra_routes=_register_catalog_route,
)
