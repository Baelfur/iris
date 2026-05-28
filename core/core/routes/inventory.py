"""View query endpoint — dynamic read layer URL surface.

All input is structurally validated: identifiers against the DDL cache,
expression-valued params (``$filter``, ``$having``) through the closed-grammar
parser in :mod:`core.engine.expression`, simple filters bound as parameters.
There is no runtime security-mode toggle — arbitrary SQL flexibility lives
in operator-authored custom queries at ``/queries/*``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ..engine.query_params import ClosedGrammarParams
from ..handlers.inventory import query_view_impl

router = APIRouter(tags=["views"])


@router.get("/{schema}/{view_name}", include_in_schema=False)
async def query_view(
    request: Request,
    schema: str,
    view_name: str,
    params: Annotated[ClosedGrammarParams, Depends()],
):
    """Query a table. All identifiers validated; expressions parsed by closed grammar."""
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
        catalog=None,
        cursor=params.cursor,
    )
