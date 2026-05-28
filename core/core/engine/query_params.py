"""Single source of truth for the closed-grammar query params.

previously three sites declared the same seven ``Query()`` shapes:
``routes/inventory.py`` for the canonical handler, ``aliases.py`` for
alias-route handlers, ``openapi_dynamic.py`` for the per-deployment
OpenAPI spec. Drift between them meant the alias could reject input
the canonical accepts or the spec could misrepresent what's available.

This module exposes:

- :class:`ClosedGrammarParams` — a FastAPI class-based dependency.
  Routes take it as ``Depends(ClosedGrammarParams)`` and access fields
  by their Python name. The constructor signature carries the
  ``Query()`` declarations so FastAPI's introspection pulls in alias,
  description, and constraints.
- :func:`openapi_dicts` — the OpenAPI parameter list for the dynamic
  spec generator. Derived from the same constructor signature so adding
  a param means editing one location.

Adding a new closed-grammar param: add a parameter to the
``ClosedGrammarParams.__init__`` signature with its ``Query(...)``
default. :func:`openapi_dicts` picks it up automatically on the next
spec build.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import Query
from fastapi.params import Query as QueryParam


class ClosedGrammarParams:
    """FastAPI dependency bundling the closed-grammar query params.

    Used as ``Depends(ClosedGrammarParams)`` in routes — handlers reach
    fields by their Python name (``params.select``, ``params.filter_``,
    etc.). The constructor's ``Query()`` defaults are the single
    declaration of alias, description, and constraints for all
    consumers.
    """

    def __init__(
        self,
        select: str | None = Query(
            None,
            alias="$select",
            description=(
                "Comma-separated columns to return. See operation description for the column list."
            ),
        ),
        filter_: str | None = Query(
            None,
            alias="$filter",
            description=(
                "WHERE clause expression in the project's closed grammar "
                "(e.g. `id eq 1 and category eq 'widget'`). Columns must "
                "be drawn from the operation description's column list."
            ),
        ),
        orderby: str | None = Query(
            None,
            alias="$orderby",
            description="ORDER BY clause (e.g. `id ASC, name DESC`).",
        ),
        count: int | None = Query(
            None,
            alias="$count",
            ge=1,
            description="Max rows to return.",
        ),
        start_index: int | None = Query(
            None,
            alias="$start_index",
            ge=0,
            description="Row offset for pagination.",
        ),
        cursor: str | None = Query(
            None,
            alias="$cursor",
            description=(
                "Opaque keyset-pagination cursor returned by a prior response's "
                "`cursor` field. Mutually exclusive with `$start_index`. Requires "
                "the same `$orderby` that produced the cursor — server rejects on "
                "mismatch. See response-shape docs for the cursor walk pattern."
            ),
        ),
        groupby: str | None = Query(
            None,
            alias="$groupby",
            description=("Comma-separated columns for GROUP BY. Requires `$select` ⊆ `$groupby`."),
        ),
        having: str | None = Query(
            None,
            alias="$having",
            description="HAVING clause expression. Requires `$groupby`.",
        ),
    ):
        self.select = select
        self.filter_ = filter_
        self.orderby = orderby
        self.count = count
        self.start_index = start_index
        self.cursor = cursor
        self.groupby = groupby
        self.having = having


def _extract_ge(q: QueryParam) -> int | None:
    """Pull a ``ge`` constraint out of a FastAPI ``Query`` default.

    Pydantic v2 stores constraints in the ``metadata`` list as
    ``annotated_types.Ge`` (and friends). Direct ``q.ge`` access from
    older FastAPI is also handled as a fallback.
    """
    if hasattr(q, "ge") and q.ge is not None:
        return q.ge
    for entry in getattr(q, "metadata", []) or []:
        ge = getattr(entry, "ge", None)
        if ge is not None:
            return ge
    return None


def openapi_dicts() -> list[dict[str, Any]]:
    """Build OpenAPI parameter dicts from ``ClosedGrammarParams``'s
    constructor signature.

    Walks ``ClosedGrammarParams.__init__`` and projects each ``Query()``
    default into the per-parameter dict shape that
    ``openapi_dynamic`` injects into route operations.
    """
    out: list[dict[str, Any]] = []
    sig = inspect.signature(ClosedGrammarParams.__init__)
    for py_name, param in sig.parameters.items():
        if py_name == "self":
            continue
        q = param.default
        if not isinstance(q, QueryParam):
            continue
        annotation = param.annotation
        is_int = "int" in str(annotation)
        schema_dict: dict[str, Any] = {"type": "integer" if is_int else "string"}
        ge = _extract_ge(q)
        if ge is not None:
            schema_dict["minimum"] = ge
        out.append(
            {
                "name": q.alias or py_name,
                "in": "query",
                "required": False,
                "schema": schema_dict,
                "description": q.description or "",
            }
        )
    return out
