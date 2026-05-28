"""Custom FastAPI exception handler producing the unified error envelope.

All HTTPException responses use the same shape as driver-error responses:
``{"error": {"code": "<class>", "message": "<text>", ...extras}}``.

Call sites pass either:

- A string detail — the most common form. The handler projects it as
  ``message`` and assigns ``code`` from the HTTP status.
- A dict detail — for sites that surface structured extras (e.g.
  ``did_you_mean``). The dict must contain ``message`` and may
  contain any number of extra keys, all of which merge into the ``error``
  envelope alongside ``code``.

Under ``ERROR_DETAIL=verbose`` the handler also adds ``deployment`` and
``database`` fields as siblings of ``error`` so multi-instance environments
can identify which instance produced the error.
"""

from fastapi import FastAPI
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..context import get_context
from .messages import add_verbose_context, code_for_status


async def _http_exception_handler(_request, exc: StarletteHTTPException):
    code = code_for_status(exc.status_code)
    if isinstance(exc.detail, dict):
        error: dict = {"code": code, **exc.detail}
    else:
        error = {"code": code, "message": exc.detail}
    body: dict = {"error": error}
    ctx = get_context()
    if ctx.settings.error_detail == "verbose":
        add_verbose_context(body, ctx)
    headers = getattr(exc, "headers", None)
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


def register(app: FastAPI) -> None:
    """Register the unified-envelope HTTPException handler on a FastAPI app.

    Variants call this from their main.py after constructing the app.
    Catches both ``fastapi.HTTPException`` and the underlying
    ``starlette.HTTPException`` — FastAPI subclasses the latter, but
    Starlette's own raises are still possible from middleware.
    """
    # FastAPI's add_exception_handler signature is widely accepted to be
    # under-typed for the HTTPException-specific handler shape we use;
    # the runtime contract is correct.
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(FastAPIHTTPException, _http_exception_handler)  # type: ignore[arg-type]
