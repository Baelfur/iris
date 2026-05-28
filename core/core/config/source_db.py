"""Postgres-backed config source.

Database-per-deployment isolation: each instance has its own Postgres
database (named after ``DEPLOYMENT_NAME``) on a shared config Postgres
server. One config Postgres can serve N deployments without co-mingling
rows or sharing RBAC.

Two well-known tables in each deployment database. The names are
namespaced by ``APP_NAME`` so forks rebrand the entire schema with one
env-var change. Default ``APP_NAME=app`` yields:

.. code-block:: sql

    CREATE TABLE app_config_validations (
        schema_name TEXT NOT NULL,
        table_name TEXT NOT NULL,
        required JSONB NOT NULL DEFAULT '[]',
        optional JSONB NOT NULL DEFAULT '[]',
        PRIMARY KEY (schema_name, table_name)
    );

    CREATE TABLE app_config_queries (
        path TEXT NOT NULL,
        sql TEXT NOT NULL,
        required JSONB NOT NULL DEFAULT '[]',
        optional JSONB NOT NULL DEFAULT '[]',
        allow_writes BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (path)
    );

Bootstrap on first ``materialize()``:

1. Connect to the admin database (``postgres``) at the configured DSN.
2. ``CREATE DATABASE <deployment_name>`` if missing — requires
   ``CREATEDB`` grant on the service account. Operator's deliberate
   one-time grant.
3. Reconnect to the deployment database.
4. ``CREATE TABLE IF NOT EXISTS`` for both tables.
5. Query rows; serialize to YAML files in a tmpdir; the existing loaders
   read from there.

Reload re-queries and re-writes the YAMLs.

The config DB is always Postgres regardless of which DB the instance is
fronting. A Trino-fronting deployment still has a Postgres config DB;
this is a deliberate consistency choice (one schema, one bootstrap, one
set of operator-facing SQL examples).
"""

import logging
import re
import tempfile
from pathlib import Path

import yaml

from .source import ConfigSource

logger = logging.getLogger(__name__)

# Same regex as AppSettings.deployment_name. Reproduced here as a
# second-line defense — the database name is interpolated into a
# CREATE DATABASE statement and Postgres doesn't accept parameterized
# identifiers there. The regex guarantees the value can't carry quotes
# or special chars. Hyphens are accepted at the input layer and
# normalized to underscores for the actual Postgres identifier.
_DEPLOYMENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")

# SQL-identifier-shaped pattern for the validated-segment helper. Schema
# and table names from config-DB rows are joined into filesystem paths;
# this regex keeps them shaped like real SQL identifiers (no slashes,
# no traversal, no shell-special chars). Quoted-identifier SQL syntax
# can carry arbitrary characters but the service doesn't support
# quoted identifiers at the URL layer, so the same constraint applies
# here.
_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")


def _validated_identifier_segment(value, field_name: str, row: dict) -> str | None:
    """Return ``value`` if it's a safe filesystem-path component for
    schema / table names, else None with an ERROR log naming the row.

    Rejects anything containing path separators, traversal segments,
    or characters outside the SQL-identifier alphabet. The config DB
    is operator-trusted input but defense-in-depth here catches
    misconfigured auto-import tooling, operator typos, and the
    compromised-config-DB case.
    """
    if not isinstance(value, str) or not _SQL_IDENT_RE.match(value):
        logger.error(
            "config DB %s=%r not a valid identifier (row=%r); skipping",
            field_name,
            value,
            row,
        )
        return None
    return value


def _validated_query_path(value, row: dict) -> str | None:
    """Return ``value`` if it's a safe relative path with no traversal,
    else None with an ERROR log.

    Query paths look like ``reports/by_id`` — multiple identifier-shaped
    segments separated by forward slashes. Reject absolute paths,
    backslashes, empty segments, traversal segments, and any segment
    that wouldn't pass the identifier validator.
    """
    if not isinstance(value, str) or not value:
        logger.error("config DB query path=%r empty/non-string (row=%r); skipping", value, row)
        return None
    if value.startswith("/") or "\\" in value:
        logger.error(
            "config DB query path=%r absolute or contains \\ (row=%r); skipping", value, row
        )
        return None
    segments = value.split("/")
    for seg in segments:
        if not seg or seg in (".", "..") or not _SQL_IDENT_RE.match(seg):
            logger.error(
                "config DB query path=%r contains invalid segment %r (row=%r); skipping",
                value,
                seg,
                row,
            )
            return None
    return value


def _within(path: Path, root: Path) -> bool:
    """Final-line check that ``path`` resolves inside ``root``.

    Per-component validation above should already catch traversal, but
    ``Path.resolve().is_relative_to(...)`` is the belt-and-braces check
    that catches anything the regex missed. Cheap, runs once per file
    materialized.
    """
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (ValueError, OSError):
        return False


_BOOTSTRAP_SQL_TEMPLATE = """
CREATE TABLE IF NOT EXISTS {validations} (
    schema_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    required JSONB NOT NULL DEFAULT '[]',
    optional JSONB NOT NULL DEFAULT '[]',
    PRIMARY KEY (schema_name, table_name)
);

CREATE TABLE IF NOT EXISTS {queries} (
    path TEXT NOT NULL,
    sql TEXT NOT NULL,
    required JSONB NOT NULL DEFAULT '[]',
    optional JSONB NOT NULL DEFAULT '[]',
    allow_writes BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (path)
);
"""

# Hyphens in APP_NAME (legal for header-slug purposes) aren't legal in
# unquoted Postgres identifiers, so the table prefix sanitizes them out
# the same way the Kafka metric prefix does.
_APP_PREFIX_SANITIZE = re.compile(r"-")


class DbSource(ConfigSource):
    """Reads validation/ and queries/ from a per-deployment Postgres."""

    def __init__(self, dsn: str, deployment_name: str, app_name: str = "app"):
        if not deployment_name:
            raise RuntimeError(
                "CONFIG__SOURCE=db requires DEPLOYMENT_NAME to be set "
                "(it's the per-deployment database name)"
            )
        if not _DEPLOYMENT_NAME_RE.match(deployment_name):
            # AppSettings already validates this, but defending the
            # SQL-interpolation site directly means a settings bypass
            # can't smuggle a bad name into CREATE DATABASE.
            raise RuntimeError(
                f"DEPLOYMENT_NAME for db source must match "
                f"^[a-z][a-z0-9_-]{{0,62}}$; got {deployment_name!r}"
            )
        if not dsn:
            raise RuntimeError("CONFIG__SOURCE=db requires CONFIG__DB_DSN to be set")
        self.dsn = dsn
        self.deployment_name = deployment_name
        self.db_name = deployment_name.replace("-", "_")
        if self.db_name != deployment_name:
            logger.info(
                "Config-DB name %r normalized from DEPLOYMENT_NAME=%r "
                "(hyphens not allowed in unquoted Postgres identifiers)",
                self.db_name,
                deployment_name,
            )
        # Table-name prefix from APP_NAME, hyphen-sanitized.
        # E.g. app_name="resource-direct" -> "resource_direct_config_*".
        prefix = _APP_PREFIX_SANITIZE.sub("_", app_name)
        self.validations_table = f"{prefix}_config_validations"
        self.queries_table = f"{prefix}_config_queries"
        self._tmpdir: Path | None = None

    @staticmethod
    def _import_psycopg():
        try:
            import psycopg  # noqa: F401

            return psycopg
        except ImportError as exc:
            raise RuntimeError(
                "CONFIG__SOURCE=db but psycopg is not installed. "
                "Install with: pip install -e './core[config-db]'"
            ) from exc

    def _ensure_database_exists(self) -> None:
        """Create the per-deployment database if missing.

        Connects to the admin db (``postgres``) with autocommit enabled
        — Postgres rejects ``CREATE DATABASE`` inside a transaction.
        """
        psycopg = self._import_psycopg()
        with (
            psycopg.connect(
                self.dsn,
                dbname="postgres",
                autocommit=True,
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (self.db_name,),
            )
            if cur.fetchone() is None:
                logger.info(
                    "Creating config database %r (first-time bootstrap)",
                    self.db_name,
                )
                # Identifier already regex-validated above; safe to
                # double-quote into the statement.
                cur.execute(f'CREATE DATABASE "{self.db_name}"')

    def _ensure_tables(self) -> None:
        """Create the two well-known tables if missing."""
        psycopg = self._import_psycopg()
        sql = _BOOTSTRAP_SQL_TEMPLATE.format(
            validations=self.validations_table,
            queries=self.queries_table,
        )
        with (
            psycopg.connect(self.dsn, dbname=self.db_name) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(sql)
            conn.commit()

    def _read_rows(self) -> tuple[list, list]:
        """Return (validations, queries) lists of dicts as currently
        stored. Empty lists when the tables exist but have no rows —
        same as a freshly-cloned empty config repo."""
        psycopg = self._import_psycopg()
        with (
            psycopg.connect(self.dsn, dbname=self.db_name) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                f"SELECT schema_name, table_name, required, optional FROM {self.validations_table}"  # noqa: S608 — identifier is validated app_name prefix
            )
            validations = [
                {
                    "schema_name": s,
                    "table_name": t,
                    "required": req or [],
                    "optional": opt or [],
                }
                for s, t, req, opt in cur.fetchall()
            ]

            cur.execute(
                f"SELECT path, sql, required, optional, allow_writes FROM {self.queries_table}"  # noqa: S608 — identifier is validated app_name prefix
            )
            queries = [
                {
                    "path": p,
                    "sql": sql,
                    "required": req or [],
                    "optional": opt or [],
                    "allow_writes": bool(aw),
                }
                for p, sql, req, opt, aw in cur.fetchall()
            ]
        return validations, queries

    def _write_yamls(self, target: Path, validations: list, queries: list) -> None:
        """Materialize the rows out as YAML files the existing loaders
        can consume. Layout matches the in-image / git-source shape:
        ``validation/<schema>/<table>.yaml`` and ``queries/<path>.yaml``.

        Row values flow from the config DB into filesystem path
        components. The DB is operator-trusted input — but operator-
        trusted isn't operator-perfect, and misconfigured auto-import
        tooling, operator typos, or a compromised config DB shouldn't
        be able to escape the temp dir via ``..`` segments. Each
        component is validated and the final resolved path is checked
        to be inside ``target``.
        """
        # Wipe any prior state so deletions in the DB land too.
        for sub in ("validation", "queries"):
            sub_path = target / sub
            if sub_path.exists():
                for child in sub_path.rglob("*"):
                    if child.is_file():
                        child.unlink()

        validation_root = target / "validation"
        for v in validations:
            schema = _validated_identifier_segment(v["schema_name"], "schema_name", v)
            table = _validated_identifier_segment(v["table_name"], "table_name", v)
            if schema is None or table is None:
                continue
            file = validation_root / schema / f"{table}.yaml"
            if not _within(file, target):
                logger.error(
                    "config DB validation row resolves outside target dir "
                    "(schema=%r, table=%r); skipping",
                    v["schema_name"],
                    v["table_name"],
                )
                continue
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(
                yaml.safe_dump(
                    {"params": {"required": v["required"], "optional": v["optional"]}},
                    default_flow_style=False,
                )
            )

        queries_root = target / "queries"
        for q in queries:
            # path is a relative path like 'reports/by_id'
            rel = _validated_query_path(q["path"], q)
            if rel is None:
                continue
            file = queries_root / f"{rel}.yaml"
            if not _within(file, target):
                logger.error(
                    "config DB query row resolves outside target dir (path=%r); skipping",
                    q["path"],
                )
                continue
            file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "sql": q["sql"],
                "params": {"required": q["required"], "optional": q["optional"]},
            }
            if q["allow_writes"]:
                payload["allow_writes"] = True
            file.write_text(yaml.safe_dump(payload, default_flow_style=False))

    def materialize(self) -> Path:
        if self._tmpdir is not None:
            return self._tmpdir
        self._ensure_database_exists()
        self._ensure_tables()
        target = Path(tempfile.mkdtemp(prefix=f"app-config-db-{self.deployment_name}-"))
        validations, queries = self._read_rows()
        self._write_yamls(target, validations, queries)
        logger.info(
            "Config DB %r materialized: %d validation row(s), %d query/queries",
            self.deployment_name,
            len(validations),
            len(queries),
        )
        self._tmpdir = target
        return target

    def reload(self) -> Path:
        if self._tmpdir is None:
            return self.materialize()
        validations, queries = self._read_rows()
        self._write_yamls(self._tmpdir, validations, queries)
        logger.info(
            "Config DB %r reloaded: %d validation row(s), %d query/queries",
            self.deployment_name,
            len(validations),
            len(queries),
        )
        return self._tmpdir
