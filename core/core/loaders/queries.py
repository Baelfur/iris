"""Custom SQL queries loaded from YAML files in queries/ directory.

Directory structure maps to URL paths:

    queries/
      reports/
        sites_by_state.yaml   -> GET /queries/reports/sites_by_state

YAML format:

    sql: |
      SELECT id, name, category
      FROM public.products
      WHERE category = :category
    params:
      required:
        - category
      optional:
        - name
    # Optional: opt into a non-SELECT statement. Default false.
    # allow_writes: true
"""

import logging
import re
from pathlib import Path

import yaml

from .contract import ParamContract

logger = logging.getLogger(__name__)

# route_path -> QueryDef
_queries: dict[str, "QueryDef"] = {}


# Strip leading whitespace and SQL comments (-- line, /* block */) before the
# read-only check so that operators can header their queries with a comment.
_LEADING_NOISE_RE = re.compile(
    r"\A(?:\s+|--[^\n]*\n?|/\*.*?\*/)*",
    re.DOTALL,
)
_READ_ONLY_HEAD_RE = re.compile(r"\A(?:select|with)\b", re.IGNORECASE)

# Tokens that indicate data modification. Scanned as whole words (\b) so
# column / table names containing the substring (`delete_at`, `created_by`)
# don't false-positive. Comments and string literals are stripped before
# the scan so a literal like 'DROP' or `-- DELETE: handle case` survives.
_DATA_MOD_KEYWORDS_RE = re.compile(
    r"\b(?:insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|replace|call|exec)\b",
    re.IGNORECASE,
)


# Comments + string literals must be removed before the data-modification
# scan; otherwise a column named in a string literal or commented-out SQL
# would false-positive. Comments come first because the string-strip
# regex would otherwise eat `--`-prefixed text inside a comment.
_STRIP_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_STRIP_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# Single-quoted string with embedded `''` escapes (SQL-92 style). Most
# dialects also support double-quoted IDENTIFIERS but those aren't
# string literals — treating them like strings would mask write
# operations on quoted table names.
_STRIP_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _strip_sql_noise(sql: str) -> str:
    """Remove SQL comments + single-quoted string literals.

    Used as a pre-pass before scanning for data-modification keywords so
    literals like ``WHERE action = 'delete'`` and comments like ``-- TODO:
    delete this`` don't false-positive against the keyword scanner.
    """
    sql = _STRIP_BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _STRIP_LINE_COMMENT_RE.sub(" ", sql)
    sql = _STRIP_STRING_LITERAL_RE.sub("''", sql)
    return sql


def _is_read_only(sql: str) -> bool:
    """Heuristic read-only check for operator-authored custom SQL.

    Two-stage:

    1. After stripping leading whitespace + comments, the statement must
       begin with ``SELECT`` or ``WITH``.
    2. After also stripping string literals + all comments, the remainder
       must not contain any data-modification keyword as a whole word
       (``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``, ``TRUNCATE``,
       ``DROP``, ``ALTER``, ``CREATE``, ``GRANT``, ``REVOKE``,
       ``REPLACE``, ``CALL``, ``EXEC``).

    Stage 2 closes the writable-CTE bypass — previously a query like
    ``WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`` passed the
    leading-token check and got loaded under ``allow_writes: false``.
    Postgres, Oracle, and Trino all support data-modifying CTEs.

    **This is a smell-test, not a security boundary.** A motivated
    operator with config-repo write access can still author a query
    that mutates state — heuristic keyword scans aren't a parser, and
    SQL is large. The real boundary is the DB grant the service
    account holds; the read-only check exists to catch *operator
    mistakes* (typoed a DELETE in a query meant to be SELECT-only) and
    to force a deliberate ``allow_writes: true`` handshake for queries
    that genuinely need to write. See ``security-posture.md`` controls 1
    and the "operator-authored SQL" residual-risks section.
    """
    stripped_head = _LEADING_NOISE_RE.sub("", sql)
    if not _READ_ONLY_HEAD_RE.match(stripped_head):
        return False
    body = _strip_sql_noise(sql)
    return _DATA_MOD_KEYWORDS_RE.search(body) is None


class QueryDef:
    """A custom SQL query loaded from YAML.

    The ``view_def`` attribute is a :class:`ParamContract` describing
    the YAML's required/optional params. The historical attribute name
    is preserved (callers and tests reference ``qdef.view_def``) even
    though the type is no longer specifically a "view definition" —
    both YAML loaders share the contract shape.

    ``aliases`` is the list of additional URL paths that should route
    to this query alongside its canonical ``/queries/<path>`` URL.
    Used for legacy-gateway migration.
    """

    def __init__(
        self,
        sql: str,
        view_def: ParamContract,
        name: str,
        aliases: list[str] | None = None,
    ):
        self.sql = sql.strip()
        self.view_def = view_def
        self.name = name
        self.aliases: list[str] = list(aliases) if aliases else []


def load_queries(queries_dir: str = "queries") -> int:
    """Scan queries/ directory for YAML definitions.

    Returns count of queries loaded. YAML files whose ``sql`` doesn't pass
    :func:`_is_read_only` are skipped with an ERROR log unless they set
    ``allow_writes: true``. The check catches operator mistakes (typoed
    DELETE / UPDATE) and writable-CTE patterns (``WITH x AS (DELETE …)
    SELECT …``) — see the function's docstring for the limits of what
    the heuristic guarantees. The endpoint becomes unreachable rather
    than crashing the pod — fail-closed without breaking the service.
    """
    global _queries
    _queries = {}
    base = Path(queries_dir)

    if not base.exists():
        logger.info("No queries/ directory found — custom queries disabled")
        return 0

    count = 0
    for yml in base.rglob("*.yaml"):
        rel = yml.relative_to(base)
        route_path = str(rel.with_suffix(""))

        with open(yml) as f:
            data = yaml.safe_load(f) or {}

        sql = data.get("sql")
        if not sql:
            logger.warning("queries/%s — missing 'sql' key, skipping", rel)
            continue

        if not data.get("allow_writes", False) and not _is_read_only(sql):
            logger.error(
                "queries/%s — SQL is not read-only (leading verb is not "
                "SELECT/WITH, or a data-modification keyword like INSERT / "
                "UPDATE / DELETE was found in the body — see writable-CTE "
                "patterns). Set 'allow_writes: true' in the YAML to opt "
                "into write queries. Skipping.",
                rel,
            )
            continue

        params = data.get("params", {})
        view_def = ParamContract(
            required=params.get("required", []),
            optional=params.get("optional", []),
        )

        aliases = data.get("aliases") or []
        if not isinstance(aliases, list):
            logger.warning(
                "queries/%s — 'aliases' must be a list; got %s. Skipping aliases.",
                rel,
                type(aliases).__name__,
            )
            aliases = []

        _queries[route_path] = QueryDef(
            sql=sql,
            view_def=view_def,
            name=yml.stem,
            aliases=[str(a) for a in aliases],
        )
        count += 1
        logger.info(
            "Loaded query: /queries/%s (required=%s, optional=%s, aliases=%s)",
            route_path,
            sorted(view_def.required),
            sorted(view_def.optional),
            _queries[route_path].aliases,
        )

    return count


def get_query(route_path: str) -> QueryDef | None:
    """Look up a custom query by its route path."""
    return _queries.get(route_path)


def list_queries() -> list[str]:
    """Return all registered query paths."""
    return sorted(_queries.keys())
