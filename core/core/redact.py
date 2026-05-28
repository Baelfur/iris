"""Obfuscate usernames in log messages via salted HMAC.

Passthrough DB errors can include the attempted username (e.g. Postgres's
`password authentication failed for user "alice"`). We don't want that landing
in log aggregators as cleartext PII — but we do want a stable, correlatable
token so operators can thread auth failures for the same user across pods and
time, and verify a candidate name matches a log entry.

Pattern: HMAC-SHA256(username, secret) truncated to 16 hex chars.
Deterministic under a fixed secret; not reversible without it; recomputable
by anyone holding the secret for audit purposes.

The secret is supplied per-call (route handlers thread
``ctx.settings.log_user_secret``) rather than read from the environment;
that keeps the configuration surface in one place — Pydantic settings
— rather than having one utility module reach into ``os.environ``
behind everyone's back. The standalone-verification one-liner below
falls back to ``os.environ`` so it remains usable without bringing up
the full settings stack:

    $ LOG_USER_SECRET=... python -c \\
        "from core.redact import hash_username; print(hash_username('alice'))"
"""

import hmac
import os
from hashlib import sha256

_REDACTED = "<redacted>"


def hash_username(username: str, secret: str | None = None) -> str:
    """Return a stable, salted hash of the username.

    Pass ``secret`` explicitly when you have a settings object in hand
    (the route handlers do — they pass ``ctx.settings.log_user_secret``).
    The ``secret=None`` fallback reads ``LOG_USER_SECRET`` from the
    environment for the standalone-verification workflow documented in
    the module docstring; production paths shouldn't rely on it.

    Returns ``'<redacted>'`` when ``secret`` is empty / missing —
    operators who don't set it lose correlation but don't leak PII.
    """
    if secret is None:
        secret = os.environ.get("LOG_USER_SECRET", "")
    if not secret or not username:
        return _REDACTED
    digest = hmac.new(secret.encode(), username.encode(), sha256).hexdigest()
    return f"user:{digest[:16]}"


def redact_username(message: str, username: str, secret: str | None = None) -> str:
    """Replace every occurrence of ``username`` in ``message`` with its hash.

    See :func:`hash_username` for the secret-passing convention.
    """
    if not username:
        return message
    return message.replace(username, hash_username(username, secret))
