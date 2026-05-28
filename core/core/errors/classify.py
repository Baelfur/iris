"""Map wrapped DatabaseError text to stable error codes for ERROR_DETAIL=safe.

The codes are an service-side contract — clients can branch on them without
seeing DB topology, role names, or constraint identifiers in response bodies.
Patterns are matched case-insensitively against the wrapped driver text
using word-boundary regex; first match wins. Falls through to
``db.query_failed`` when nothing matches.

Coverage targets the failure classes that show up in real read-only
deployments: passthrough auth failures, DB unreachability, role-grant gaps,
and the per-query timeout (``QUERY_TIMEOUT_SECONDS``). New patterns belong
here; routes consume the pair via :func:`classify`.

**Residual mis-classification risk**: word boundaries (``\\b``) make
identifier-like collisions inert (a column named ``permission_denied_at``
or ``connection_refused_count`` won't match), but a driver error whose
text echoes a pattern phrase verbatim (e.g. user-supplied data quoted in
the error message) would still classify. Per the open-list contract
documented in security-posture.md control 10, clients must treat the
``db.*`` codes as best-effort — fall back to ``db.query_failed``-equivalent
behavior when a code is unexpected.
"""

import re

# (compiled_pattern, code, human_message) — patterns use \b boundaries so
# identifier-like collisions (e.g. a column named ``connection_refused_at``)
# don't mis-fire. Patterns are case-insensitive.
_PATTERNS: tuple[tuple[re.Pattern, str, str], ...] = (
    # --- Authentication failures (pool credential or X-DB-Authorization) ---
    (
        re.compile(r"\bpassword authentication failed\b", re.IGNORECASE),
        "db.bad_credentials",
        "Database rejected the supplied credentials",
    ),
    (
        re.compile(r"\baccess denied for user\b", re.IGNORECASE),
        "db.bad_credentials",
        "Database rejected the supplied credentials",
    ),
    (
        re.compile(r"\bora-01017\b", re.IGNORECASE),
        "db.bad_credentials",
        "Database rejected the supplied credentials",
    ),
    (
        re.compile(r"\bauthentication failed\b", re.IGNORECASE),
        "db.bad_credentials",
        "Database rejected the supplied credentials",
    ),
    # --- Network / DB reachability ---
    (
        re.compile(r"\bcould not connect to server\b", re.IGNORECASE),
        "db.connection_refused",
        "Could not reach the database",
    ),
    (
        re.compile(r"\bconnection refused\b", re.IGNORECASE),
        "db.connection_refused",
        "Could not reach the database",
    ),
    (
        re.compile(r"\bora-12170\b", re.IGNORECASE),
        "db.connection_refused",
        "Could not reach the database",
    ),
    (
        re.compile(r"can't connect to mysql", re.IGNORECASE),
        "db.connection_refused",
        "Could not reach the database",
    ),
    # NOTE: "connection reset by peer" is intentionally NOT classified as
    # connection_refused — it can fire mid-query when the server kills a
    # session that was already up, which is a different failure mode.
    # Falls through to db.query_failed; clients should retry either way.
    # --- Authorization (role grants insufficient) ---
    (
        re.compile(r"\bpermission denied for\b", re.IGNORECASE),
        "db.permission_denied",
        "Database denied access to the resource",
    ),
    (
        re.compile(r"\bora-00942\b", re.IGNORECASE),
        "db.permission_denied",
        "Database denied access to the resource",
    ),
    (
        re.compile(r"\bora-01031\b", re.IGNORECASE),
        "db.permission_denied",
        "Database denied access to the resource",
    ),
    (
        re.compile(r"\bcommand denied to user\b", re.IGNORECASE),
        "db.permission_denied",
        "Database denied access to the resource",
    ),
    # --- Per-query timeout (QUERY_TIMEOUT_SECONDS fired DB-side) ---
    (
        re.compile(r"\bstatement timeout\b", re.IGNORECASE),
        "db.timeout",
        "Query exceeded the configured timeout",
    ),
    (
        re.compile(r"\bmax_statement_time\b", re.IGNORECASE),
        "db.timeout",
        "Query exceeded the configured timeout",
    ),
    (
        re.compile(r"\bmax_execution_time\b", re.IGNORECASE),
        "db.timeout",
        "Query exceeded the configured timeout",
    ),
    (
        re.compile(r"\bora-01013\b", re.IGNORECASE),
        "db.timeout",
        "Query exceeded the configured timeout",
    ),
    (
        re.compile(r"\bquery exceeded the maximum execution time\b", re.IGNORECASE),
        "db.timeout",
        "Query exceeded the configured timeout",
    ),
)

_FALLBACK: tuple[str, str] = ("db.query_failed", "Database query failed")


def classify(detail: str) -> tuple[str, str]:
    """Return ``(code, human_message)`` for a wrapped driver error string."""
    for pattern, code, message in _PATTERNS:
        if pattern.search(detail):
            return code, message
    return _FALLBACK
