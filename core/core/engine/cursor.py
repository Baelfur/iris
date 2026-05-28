"""HMAC-signed opaque cursors for keyset pagination.

A cursor token encodes the orderby clause that produced a page plus the
values of those columns on the page's last row. The next request's
keyset ``WHERE`` clause uses those values to fetch only rows that come
after the last seen one — O(log n) per page given a real index, vs the
O(n²) total walk of offset pagination.

## Token format

``base64url(payload_json).base64url(hmac_sha256(payload_json))``

where ``payload_json`` is::

    {"orderby": "<normalized clause>", "values": [<last-row col values>]}

The signature binds the payload to the configured ``cursor_secret`` so
callers can't fabricate a cursor that bypasses the original WHERE
clause or the server-side validation that follows. Operators rotating
the secret invalidates all in-flight cursors — acceptable because
cursors are short-lived by design.

## Orderby normalization

The orderby string baked into the token is the *normalized* form the
handler emits (canonical column case, single space between column and
direction, comma-separated). The request-side check compares the
incoming ``$orderby`` (also normalized via the same path) against the
token's clause; a mismatch is rejected with 400 because the keyset
WHERE would be meaningless if the order changed mid-walk.

## Out of scope

- Backwards walking (``prev`` cursor) — agent walks are forward-only;
  defer until a real consumer asks.
- Cursor expiry / TTL — stateless tokens never expire by design.
- NULL ordering — columns in ``$orderby`` should be NOT NULL; NULL
  values may skip rows or duplicate across pages depending on the DB's
  ordering rules. The using-guide docs the constraint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any


class CursorError(ValueError):
    """Raised when a cursor fails to decode, verify, or match the request."""


# Cached per-process random fallback when settings.cursor_secret is unset.
# Cursors signed with this key don't survive process restarts or load-balance
# across pod replicas — operators running >1 replica must set the secret
# explicitly. The startup logger emits a warning in that case.
_random_fallback: bytes | None = None


def get_secret(configured: str) -> bytes:
    """Return the bytes HMAC key from the configured setting, or a cached
    per-process random key when unset.

    The configured value is taken as a UTF-8 string and used as raw bytes
    — no hex decode. Operators typically supply 32+ random ASCII chars
    or 64 hex chars; either form is acceptable as long as the secret is
    long enough to resist offline brute-force against the HMAC.
    """
    if configured:
        return configured.encode("utf-8")
    global _random_fallback
    if _random_fallback is None:
        _random_fallback = secrets.token_bytes(32)
    return _random_fallback


def _b64u_encode(data: bytes) -> str:
    """URL-safe base64 without padding (RFC 7515 §2 style)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    """Inverse of :func:`_b64u_encode`. Restores stripped padding."""
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def make_cursor(orderby_normalized: str, last_row_values: list[Any], secret: bytes) -> str:
    """Encode + sign a cursor for the given orderby + last-row values.

    Args:
        orderby_normalized: The normalized ``$orderby`` clause that
            produced this page (e.g., ``"id ASC"`` or ``"category ASC,
            id ASC"``). Stored verbatim in the token; the request-side
            check compares it against the next request's normalized
            ``$orderby``.
        last_row_values: Values from the page's last row, in the same
            order as the orderby columns. Must be JSON-serializable
            (str / int / float / bool / None / nested list/dict).
        secret: HMAC key from ``settings.cursor_secret``. Treated as raw
            bytes — operators set a long random hex string and it gets
            decoded upstream into this bytes form, or the settings layer
            generates a per-process random key if unset.

    Returns the ``payload.signature`` cursor string.
    """
    payload = json.dumps(
        {"orderby": orderby_normalized, "values": last_row_values},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64u_encode(payload)}.{_b64u_encode(sig)}"


def parse_cursor(
    token: str,
    expected_orderby_normalized: str,
    secret: bytes,
) -> list[Any]:
    """Verify a cursor and return the last-row values.

    Args:
        token: The opaque cursor string from ``?$cursor=``.
        expected_orderby_normalized: The normalized ``$orderby`` of the
            current request. Must match the clause encoded in the token
            — a mismatch (or no ``$orderby`` on the current request)
            means the keyset comparison would be incoherent.
        secret: HMAC key. Same value used to sign.

    Returns the decoded list of last-row values.

    Raises:
        CursorError: on structural failure (bad format, missing parts),
            signature failure (tamper / wrong secret), or orderby
            mismatch.
    """
    if not token or "." not in token:
        raise CursorError("cursor format invalid")
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        payload = _b64u_decode(payload_b64)
        sig = _b64u_decode(sig_b64)
    except ValueError as exc:
        # base64 errors raise binascii.Error which is a ValueError subclass —
        # one except clause catches both wire-level decode failures.
        raise CursorError("cursor base64 decode failed") from exc

    expected_sig = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        raise CursorError("cursor signature does not verify")

    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("cursor payload not valid JSON") from exc

    if not isinstance(body, dict) or "orderby" not in body or "values" not in body:
        raise CursorError("cursor payload missing required fields")

    if body["orderby"] != expected_orderby_normalized:
        raise CursorError(
            "cursor was issued for a different $orderby; restart pagination "
            "with the original $orderby or omit $cursor"
        )

    values = body["values"]
    if not isinstance(values, list):
        raise CursorError("cursor values not a list")
    return values
