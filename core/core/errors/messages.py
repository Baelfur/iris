"""Error response helpers — both produce the unified envelope shape:
``{"error": {"code": "<class>", "message": "<text>", ...extras}}``.

Verbose mode adds operator-debug fields (``deployment``, ``database``) as
siblings of ``error``.

``error_msg`` shapes the human string used as a validation-time HTTPException
detail (terse vs verbose). ``db_error_body`` shapes the JSON body returned
when ``DatabaseError`` bubbles out of a route. The unified envelope
means consumers branch on ``error.code`` instead of ``detail`` vs ``error``.
"""

from typing import TYPE_CHECKING

from ..context import get_context
from .classify import classify

if TYPE_CHECKING:
    from ..context import AppContext


# Standard terse-mode titles. Use these in ``error_msg`` calls so the
# ``ERROR_DETAIL=terse`` response shape stays consistent across auth,
# admin, and route surfaces.
TITLE_BAD_REQUEST = "Bad request"
TITLE_NOT_FOUND = "Not found"
TITLE_UNAUTHORIZED = "Unauthorized"
TITLE_FORBIDDEN = "Forbidden"


# HTTP status -> envelope code mapping. Used by the HTTPException handler
# to project validation/auth errors into the unified envelope.
_STATUS_CODES = {
    400: "validation.bad_request",
    401: "auth.unauthorized",
    403: "auth.forbidden",
    404: "validation.not_found",
    409: "validation.conflict",
    422: "validation.unprocessable",
}


def code_for_status(status_code: int) -> str:
    """Return the stable error code for an HTTPException status.

    5xx falls through to ``server.error``; unknown 4xx falls through to
    ``validation.bad_request`` (since they're operator-facing rule
    violations under our handler's contract).
    """
    if status_code in _STATUS_CODES:
        return _STATUS_CODES[status_code]
    if 500 <= status_code < 600:
        return "server.error"
    return "validation.bad_request"


def error_msg(verbose_msg: str, terse_msg: str = TITLE_BAD_REQUEST) -> str:
    """Return verbose-or-terse string for validation-time HTTPException details.

    ``safe`` mode collapses to the terse string here — codes only make
    sense for driver errors, not validation failures the operator
    already understands from the URL grammar.
    """
    if get_context().settings.error_detail == "verbose":
        return verbose_msg
    return terse_msg


def db_error_body(detail: str) -> dict:
    """Build the unified JSON response body for an ``DatabaseError``.

    All three ERROR_DETAIL modes return the same envelope shape; only
    code, message, and verbose-context fields vary:

    - ``terse`` (default) → ``{"error": {"code": "db.query_failed",
      "message": "Query failed"}}``. Generic — operator information stays
      out of the response body.
    - ``safe`` → ``{"error": {"code": "db.<class>", "message": "<safe>"}}``
      where ``code`` is from :mod:`.classify`. Stable shape for machine
      consumers; no topology, no debug context.
    - ``verbose`` → ``{"error": {"code": "db.<class>", "message": "<raw>"},
      "deployment": "...", "database": "..."}``. Raw driver text plus
      operator-debug context — leaks topology, so dev / inside-trust-
      perimeter only. The ``X-{App}-Deployment`` response header (
      already carries deployment identity for correlation.

    Logs always retain the full driver text regardless of mode.
    """
    ctx = get_context()
    mode = ctx.settings.error_detail
    if mode == "verbose":
        code, _ = classify(detail)
        body: dict = {"error": {"code": code, "message": detail}}
        add_verbose_context(body, ctx)
        return body
    if mode == "safe":
        code, message = classify(detail)
        return {"error": {"code": code, "message": message}}
    return {"error": {"code": "db.query_failed", "message": "Query failed"}}


def add_verbose_context(body: dict, ctx: "AppContext") -> None:
    """Add operator-debug fields to a verbose-mode error body.

    Used by both ``db_error_body`` (driver-error path) and the custom
    HTTPException handler (validation-error path) so the verbose-mode
    body shape is consistent across both surfaces.

    ``deployment`` only when ``DEPLOYMENT_NAME`` is set — keeps bodies
    backwards-compatible for deployments that don't use the env var.
    ``database`` is always available from the AppContext.
    """
    if ctx.settings.deployment_name:
        body["deployment"] = ctx.settings.deployment_name
    body["database"] = ctx.database
