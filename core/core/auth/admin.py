"""Admin authentication for ``/admin/*`` endpoints.

Two coexisting credential paths:

1. **Shared token** (``X-Admin-Token``) — the original path. A static
   secret on the header, validated constant-time against
   ``AUTH__ADMIN_TOKEN``. Typical use: CD pipelines / runbooks /
   gateway-injected tokens (see operating.md "Gating the admin lane
   through a gateway").

2. **JWT with admin claim** — added. A Bearer JWT whose
   configured claim contains the configured admin group value.
   Validated against ``AUTH__JWKS_URL`` (same JWKS as the user-facing
   JWT lane). Typical use: interactive admin access via SSO without a
   gateway in front. Settings unset = path disabled; today's behavior
   bit-for-bit.

The :func:`verify_admin_access` dispatcher routes to whichever path
the request supplies headers for. When both are present the token
wins — covers the case where a gateway injected the admin token and
the same caller happens to also carry a Bearer JWT.

Failure modes:

- No ``X-Admin-Token`` and no Bearer JWT → 401 (today's default).
- ``X-Admin-Token`` invalid → 401.
- Bearer JWT invalid → 401.
- Bearer JWT valid but no admin claim → **403** (caller authenticated
  but isn't authorized). Mild info disclosure that admin uses
  claim-checks, but standard OAuth practice.

Fail-closed posture preserved: both paths reject when their respective
settings are unset.
"""

import hmac
import logging

import jwt
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from ..context import get_context
from ..errors.messages import TITLE_FORBIDDEN, TITLE_UNAUTHORIZED, error_msg

logger = logging.getLogger(__name__)

ADMIN_TOKEN_HEADER = "x-admin-token"

# Lazy-init JWKS client for admin-JWT verification. Kept independent
# from the user-facing JWT lane's client even when both point at the
# same URL — the two paths have different failure modes and could in a
# future refactor point at different IDPs.
_admin_jwks_client: PyJWKClient | None = None
_admin_bearer = HTTPBearer(auto_error=False)


def verify_admin_token(request: Request) -> None:
    """Validate the X-Admin-Token header against the configured admin token.

    Raises HTTPException 401 on missing, wrong, or unconfigured token. Uses
    ``hmac.compare_digest`` so the comparison is constant-time and doesn't
    leak token length via early exit.
    """
    configured = get_context().settings.auth.admin_token
    if not configured:
        raise HTTPException(401, error_msg("admin token not configured", TITLE_UNAUTHORIZED))

    supplied = request.headers.get(ADMIN_TOKEN_HEADER, "")
    if not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(401, error_msg("invalid admin token", TITLE_UNAUTHORIZED))


def _get_admin_jwks_client() -> PyJWKClient | None:
    """Lazy-init the admin-JWT JWKS client on first use."""
    global _admin_jwks_client
    settings = get_context().settings
    if _admin_jwks_client is None and settings.auth.jwks_url:
        _admin_jwks_client = PyJWKClient(settings.auth.jwks_url)
    return _admin_jwks_client


def _claim_has_admin_group(payload: dict, claim_name: str, admin_group: str) -> bool:
    """True when ``payload[claim_name]`` includes ``admin_group``.

    Handles three claim shapes seen in the wild:

    - List of strings (Keycloak, Auth0 default): ``"groups": ["admin", "user"]``
    - Single string (simple role claim): ``"role": "admin"``
    - Space-delimited string (OAuth ``scope`` claim, handled specially):
      ``"scope": "read write admin"``
    """
    val = payload.get(claim_name)
    if val is None:
        return False
    if isinstance(val, list):
        return admin_group in val
    if isinstance(val, str):
        if claim_name == "scope":
            return admin_group in val.split()
        return val == admin_group
    return False


async def verify_admin_via_jwt(request: Request) -> dict:
    """Validate a Bearer JWT and require the configured admin claim.

    Returns the decoded payload (callers may log the resolved identity
    for per-user audit). Raises 401 for bad/missing tokens or
    misconfigured ``admin_group``; 403 when authenticated but missing
    the admin claim.
    """
    settings = get_context().settings
    if not settings.auth.admin_group:
        raise HTTPException(
            401,
            error_msg("admin JWT path not configured", TITLE_UNAUTHORIZED),
        )

    credentials: HTTPAuthorizationCredentials | None = await _admin_bearer(request)
    if not credentials:
        raise HTTPException(401, error_msg("Missing Authorization header", TITLE_UNAUTHORIZED))

    client = _get_admin_jwks_client()
    if client is None:
        # AuthSettings validator should have caught this at startup;
        # defensive against runtime config drift.
        raise HTTPException(
            401, error_msg("admin JWT path misconfigured (no JWKS)", TITLE_UNAUTHORIZED)
        )

    try:
        signing_key = client.get_signing_key_from_jwt(credentials.credentials)
        payload = jwt.decode(
            credentials.credentials,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.auth.audience or None,
            issuer=settings.auth.issuer or None,
            options={
                "verify_aud": bool(settings.auth.audience),
                "verify_iss": bool(settings.auth.issuer),
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(401, error_msg("Token expired", TITLE_UNAUTHORIZED)) from exc
    except jwt.InvalidTokenError as exc:
        logger.warning("Admin JWT invalid: %s", str(exc))
        raise HTTPException(401, error_msg("Invalid token", TITLE_UNAUTHORIZED)) from exc

    if not _claim_has_admin_group(
        payload, settings.auth.admin_claim_name, settings.auth.admin_group
    ):
        # 403 = authenticated but not authorized. Specifics at WARNING;
        # public body stays generic so callers don't learn which claim
        # we look in.
        logger.warning(
            "Admin JWT missing required claim: sub=%s claim=%s required=%s present=%s",
            payload.get("sub", "<unknown>"),
            settings.auth.admin_claim_name,
            settings.auth.admin_group,
            payload.get(settings.auth.admin_claim_name, "<absent>"),
        )
        raise HTTPException(403, error_msg("Admin access denied", TITLE_FORBIDDEN))

    # Resolved-identity log line for the audit trail — the win this
    # whole feature buys vs the shared-token path.
    logger.info(
        "Admin JWT accepted: sub=%s",
        payload.get("sub", "<unknown>"),
    )

    return payload


async def verify_admin_access(request: Request) -> None:
    """Admin gate dispatcher — accepts either ``X-Admin-Token`` or a
    Bearer JWT with the configured admin claim.

    - ``X-Admin-Token`` header present → token path (existing behavior).
      Gateway-injected token always wins over a user-supplied JWT in
      the same request.
    - ``X-Admin-Token`` absent, ``Authorization`` header present →
      JWT path. Requires ``admin_group`` configured.
    - Neither header → 401 (today's fail-closed default).
    """
    if request.headers.get(ADMIN_TOKEN_HEADER):
        verify_admin_token(request)
        return

    if request.headers.get("authorization"):
        await verify_admin_via_jwt(request)
        return

    raise HTTPException(401, error_msg("Admin credentials required", TITLE_UNAUTHORIZED))
