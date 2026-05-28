"""Optional JWT Bearer token authentication."""

import logging

import jwt
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from ..context import get_context
from ..errors.messages import TITLE_FORBIDDEN, TITLE_UNAUTHORIZED, error_msg

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient | None:
    """Lazy-init the JWKS client on first use.

    The settings model enforces that ``jwks_url`` is set whenever
    ``mode == "jwt"``; this guard is belt-and-braces.
    """
    global _jwks_client
    settings = get_context().settings
    if _jwks_client is None and settings.auth.jwks_url:
        _jwks_client = PyJWKClient(settings.auth.jwks_url)
    return _jwks_client


async def verify_token(request: Request) -> dict | None:
    """Validate JWT Bearer token if ``AUTH__MODE=jwt``.

    Returns decoded token payload, or None when the configured mode
    doesn't require service-side validation (``gateway`` / ``open``).
    Raises HTTPException 401/403 on invalid tokens.
    """
    settings = get_context().settings
    if settings.auth.mode != "jwt":
        return None

    credentials: HTTPAuthorizationCredentials | None = await security(request)
    if not credentials:
        raise HTTPException(401, error_msg("Missing Authorization header", TITLE_UNAUTHORIZED))

    token = credentials.credentials
    client = _get_jwks_client()
    if client is None:
        # Defensive — the early-return at jwks_url-empty above covers
        # this in practice, but mypy can't see through the lazy init.
        return None

    try:
        signing_key = client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.auth.audience or None,
            issuer=settings.auth.issuer or None,
            options={
                "verify_aud": bool(settings.auth.audience),
                "verify_iss": bool(settings.auth.issuer),
            },
        )
        return payload
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(401, error_msg("Token expired", TITLE_UNAUTHORIZED)) from exc
    except jwt.InvalidTokenError as exc:
        logger.warning("Invalid token: %s", str(exc))
        raise HTTPException(403, error_msg(f"Invalid token: {exc}", TITLE_FORBIDDEN)) from exc
