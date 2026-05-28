"""Extract Basic credentials from a request for DB passthrough.

Two header forms are accepted, in priority order:

1. ``X-DB-Authorization: Basic <b64>`` — explicit passthrough header. Use
   this when JWT auth is also configured and you need to send both
   ``Authorization: Bearer <jwt>`` (the service's auth) AND DB credentials on the
   same call. The two headers stay independent.
2. ``Authorization: Basic <b64>`` — standard Basic auth. Only consulted
   when ``X-DB-Authorization`` is absent. This form is what Swagger UI's
   native ``http: basic`` security scheme produces, so deployments
   without JWT auth get the friendly username/password Authorize dialog
   without having to hand-encode anything.

The two-header arrangement preserves "JWT for API-level identity, Basic
for DB-level identity" in deployments that need both, while keeping the
single-header case ergonomic. The runtime priority — ``X-DB-Authorization``
wins when both are present — means a deployment that already uses the
explicit form keeps working exactly as before.
"""

import base64

from fastapi import Request

DB_CREDS_HEADER = "x-db-authorization"
STANDARD_AUTH_HEADER = "authorization"


def _decode_basic(value: str) -> tuple[str, str] | None:
    """Parse a ``Basic <b64>`` header value into ``(user, password)``."""
    if not value.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(value.split(" ", 1)[1]).decode("utf-8")
        user, password = decoded.split(":", 1)
        return (user, password)
    except Exception:
        return None


def extract_basic_creds(request: Request) -> tuple[str, str] | None:
    """Return ``(username, password)`` if the request carries DB-passthrough
    Basic credentials on either ``X-DB-Authorization`` (preferred) or the
    standard ``Authorization`` header, else ``None``.

    The standard-Authorization fallback is only meaningful in deployments
    where ``AUTH__JWKS_URL`` is unset — when JWT auth is configured,
    ``verify_token`` runs first and rejects any ``Authorization`` header
    that isn't ``Bearer ...``, so a Basic value on that header never
    reaches passthrough extraction.
    """
    explicit = _decode_basic(request.headers.get(DB_CREDS_HEADER, ""))
    if explicit is not None:
        return explicit
    return _decode_basic(request.headers.get(STANDARD_AUTH_HEADER, ""))
