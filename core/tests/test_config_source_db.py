"""Tests for core.config.source_db.DbSource.

Three layers of coverage, matching the pattern established for metrics
and git source:

1. **Construction validation** — deployment_name required, regex-checked,
   DSN required. Pure-Python; no psycopg involved.
2. **Mocked psycopg** — verifies the right SQL gets called in the right
   order (database existence check, CREATE DATABASE, CREATE TABLE,
   SELECT). Catches "we forgot a step" but not "the SQL is wrong."
3. **Real Postgres** — only runs when ``TEST_CONFIG_DSN`` is set
   (or the test-infra default Postgres at localhost:5432 is reachable).
   Materializes against a real database, asserts the YAML files land
   with the expected content. Catches API drift and SQL syntax bugs.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from core.config import source_db
from core.config.source_db import DbSource

# ---------------------------------------------------------------- construction


class TestConstruction:
    def test_deployment_name_required(self):
        with pytest.raises(RuntimeError, match="DEPLOYMENT_NAME"):
            DbSource(dsn="postgresql://localhost/postgres", deployment_name="")

    def test_dsn_required(self):
        with pytest.raises(RuntimeError, match="CONFIG__DB_DSN"):
            DbSource(dsn="", deployment_name="inventory")

    def test_invalid_deployment_name_rejected(self):
        """Second-line defense — even if a settings bypass lets a bad
        name through, the constructor stops it before the regex-validated
        name reaches a CREATE DATABASE statement."""
        for bad in ("Inventory", "1inv", "inv;DROP", "inv space"):
            with pytest.raises(RuntimeError, match=r"\^\[a-z\]"):
                DbSource(
                    dsn="postgresql://localhost/postgres",
                    deployment_name=bad,
                )

    def test_hyphen_normalized_for_db_name(self):
        """DEPLOYMENT_NAME accepts hyphens; the actual config-DB
        database name uses underscores (Postgres unquoted identifier
        rules). The raw deployment_name stays available for
        header/log passthrough."""
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory-demo",
        )
        assert s.deployment_name == "inventory-demo"
        assert s.db_name == "inventory_demo"

    def test_no_normalization_when_no_hyphen(self):
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory_prod",
        )
        assert s.deployment_name == "inventory_prod"
        assert s.db_name == "inventory_prod"


class TestConfigTablePrefix:
    """Table names cascade from APP_NAME so forks rebrand the schema
    with one env-var change. (#322)"""

    def test_default_app_name_yields_app_prefix(self):
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory",
        )
        # Default app_name='app' → app_config_validations / app_config_queries
        assert s.validations_table == "app_config_validations"
        assert s.queries_table == "app_config_queries"

    def test_iris_brand_restores_historical_names(self):
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory",
            app_name="iris",
        )
        assert s.validations_table == "iris_config_validations"
        assert s.queries_table == "iris_config_queries"

    def test_hyphenated_brand_sanitized(self):
        # Hyphens in APP_NAME aren't legal in Postgres unquoted
        # identifiers; the prefix sanitizes them out.
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory",
            app_name="resource-direct",
        )
        assert s.validations_table == "resource_direct_config_validations"
        assert s.queries_table == "resource_direct_config_queries"


# ---------------------------------------------------------------- missing dep


class TestMissingPsycopg:
    def test_helpful_error_when_psycopg_not_installed(self):
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory",
        )
        with patch.dict("sys.modules", {"psycopg": None}):
            with pytest.raises(RuntimeError, match=r"core\[config-db\]"):
                s.materialize()


# ---------------------------------------------------------------- mocked psycopg


class _FakeCursor:
    """Minimal cursor mock. Records executed SQL + params; returns
    pre-canned results from a queue based on which query is running."""

    def __init__(self, fake_results: dict):
        self.executed: list = []
        self._results = fake_results
        self._last_sql = ""

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last_sql = sql

    def fetchone(self):
        for key, val in self._results.items():
            if key in self._last_sql:
                if isinstance(val, list):
                    return val[0] if val else None
                return val
        return None

    def fetchall(self):
        for key, val in self._results.items():
            if key in self._last_sql:
                return val
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _fake_psycopg(cursor: _FakeCursor):
    """Build a fake psycopg module whose connect() returns a context
    manager wrapping a fake connection."""
    fake = MagicMock()
    fake.connect.return_value = _FakeConn(cursor)
    return fake


class TestMockedBootstrap:
    def test_creates_database_when_missing(self):
        cursor = _FakeCursor(fake_results={
            "pg_database": None,  # database doesn't exist yet
        })
        fake = _fake_psycopg(cursor)
        with patch.dict("sys.modules", {"psycopg": fake}):
            s = DbSource(
                dsn="postgresql://localhost/postgres",
                deployment_name="inventory",
            )
            s._ensure_database_exists()

        statements = [sql for sql, _ in cursor.executed]
        # Existence check first.
        assert any("pg_database" in sql for sql in statements)
        # Then CREATE DATABASE with the deployment name double-quoted.
        assert any('CREATE DATABASE "inventory"' in sql for sql in statements)

    def test_skips_create_when_database_exists(self):
        cursor = _FakeCursor(fake_results={
            "pg_database": [(1,)],  # database already exists
        })
        fake = _fake_psycopg(cursor)
        with patch.dict("sys.modules", {"psycopg": fake}):
            s = DbSource(
                dsn="postgresql://localhost/postgres",
                deployment_name="inventory",
            )
            s._ensure_database_exists()

        statements = [sql for sql, _ in cursor.executed]
        assert any("pg_database" in sql for sql in statements)
        assert not any("CREATE DATABASE" in sql for sql in statements)

    def test_admin_connect_uses_postgres_dbname_with_autocommit(self):
        """CREATE DATABASE can't run inside a transaction — must use
        autocommit. And we connect to admin db, not the deployment db."""
        cursor = _FakeCursor(fake_results={"pg_database": [(1,)]})
        fake = _fake_psycopg(cursor)
        with patch.dict("sys.modules", {"psycopg": fake}):
            s = DbSource(
                dsn="postgresql://localhost/postgres",
                deployment_name="inventory",
            )
            s._ensure_database_exists()

        kwargs = fake.connect.call_args.kwargs
        assert kwargs["dbname"] == "postgres"
        assert kwargs["autocommit"] is True


# ---------------------------------------------------------------- yaml writing


class TestWriteYamls:
    """The DB → YAML conversion is pure (no psycopg needed). Verify it
    produces files in the layout the existing loaders expect."""

    def test_validations_written_to_validation_subdir(self, tmp_path):
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory",
        )
        validations = [
            {"schema_name": "public", "table_name": "products",
             "required": ["id"], "optional": ["status"]},
        ]
        s._write_yamls(tmp_path, validations, queries=[])

        f = tmp_path / "validation" / "public" / "products.yaml"
        assert f.exists()
        loaded = yaml.safe_load(f.read_text())
        assert loaded == {
            "params": {"required": ["id"], "optional": ["status"]},
        }

    def test_queries_written_with_full_path(self, tmp_path):
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory",
        )
        queries = [{
            "path": "reports/by_id",
            "sql": "SELECT * FROM t WHERE id = :id",
            "required": ["id"],
            "optional": [],
            "allow_writes": False,
        }]
        s._write_yamls(tmp_path, validations=[], queries=queries)

        f = tmp_path / "queries" / "reports" / "by_id.yaml"
        assert f.exists()
        loaded = yaml.safe_load(f.read_text())
        assert loaded["sql"] == "SELECT * FROM t WHERE id = :id"
        assert loaded["params"] == {"required": ["id"], "optional": []}
        # allow_writes only emitted when true — keeps default-off
        # YAMLs minimal.
        assert "allow_writes" not in loaded

    def test_allow_writes_emitted_when_true(self, tmp_path):
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory",
        )
        queries = [{
            "path": "ops/cleanup",
            "sql": "DELETE FROM t WHERE old",
            "required": [], "optional": [],
            "allow_writes": True,
        }]
        s._write_yamls(tmp_path, validations=[], queries=queries)
        loaded = yaml.safe_load(
            (tmp_path / "queries" / "ops" / "cleanup.yaml").read_text(),
        )
        assert loaded["allow_writes"] is True

    def test_reload_wipes_stale_files(self, tmp_path):
        """If a row is deleted from the DB, the stale YAML on disk must
        not survive into the next load — that would re-register a
        deleted view-def or query."""
        s = DbSource(
            dsn="postgresql://localhost/postgres",
            deployment_name="inventory",
        )
        # First write
        s._write_yamls(tmp_path, validations=[
            {"schema_name": "public", "table_name": "products",
             "required": [], "optional": []},
            {"schema_name": "public", "table_name": "orders",
             "required": [], "optional": []},
        ], queries=[])
        assert (tmp_path / "validation" / "public" / "products.yaml").exists()
        assert (tmp_path / "validation" / "public" / "orders.yaml").exists()

        # Second write: orders removed from the DB
        s._write_yamls(tmp_path, validations=[
            {"schema_name": "public", "table_name": "products",
             "required": [], "optional": []},
        ], queries=[])
        assert (tmp_path / "validation" / "public" / "products.yaml").exists()
        assert not (tmp_path / "validation" / "public" / "orders.yaml").exists()


class TestPathTraversalRejection:
    """Defense-in-depth — config-DB row values flow into filesystem path
    components. Per-component validation + final is_relative_to check
    rejects traversal segments, absolute paths, and shell-special chars
    so a compromised config DB / misconfigured auto-import tool / operator
    typo can't escape the temp dir. (#343)"""

    def _src(self):
        return source_db.DbSource(
            dsn="host=h user=u password=p dbname=postgres",
            deployment_name="test",
            app_name="app",
        )

    def test_validation_traversal_in_schema_rejected(self, tmp_path, caplog):
        src = self._src()
        validations = [
            {"schema_name": "../escape", "table_name": "orders",
             "required": [], "optional": []},
            # A legitimate row that follows — must still be written.
            {"schema_name": "public", "table_name": "products",
             "required": [], "optional": []},
        ]
        src._write_yamls(tmp_path, validations, queries=[])
        assert (tmp_path / "validation" / "public" / "products.yaml").exists()
        # The traversal row shouldn't have produced anything outside tmp_path.
        assert not (tmp_path.parent / "escape").exists()
        assert "not a valid identifier" in caplog.text

    def test_validation_traversal_in_table_rejected(self, tmp_path, caplog):
        src = self._src()
        validations = [
            {"schema_name": "public", "table_name": "../../etc/passwd",
             "required": [], "optional": []},
        ]
        src._write_yamls(tmp_path, validations, queries=[])
        # Anywhere a YAML file might have landed → didn't.
        assert not (tmp_path / "validation" / "public" / "../../etc/passwd.yaml").exists()
        assert not Path("/etc/passwd.yaml").exists() or Path("/etc/passwd.yaml").read_text() != ""
        assert "not a valid identifier" in caplog.text

    def test_validation_slash_in_table_rejected(self, tmp_path, caplog):
        src = self._src()
        validations = [
            {"schema_name": "public", "table_name": "orders/sub",
             "required": [], "optional": []},
        ]
        src._write_yamls(tmp_path, validations, queries=[])
        # No spurious file written under the parent slash-split path.
        assert not (tmp_path / "validation" / "public" / "orders" / "sub.yaml").exists()
        assert "not a valid identifier" in caplog.text

    def test_query_path_traversal_rejected(self, tmp_path, caplog):
        src = self._src()
        queries = [
            {"path": "../../etc/cron.d/evil", "sql": "SELECT 1",
             "required": [], "optional": [], "allow_writes": False},
            {"path": "reports/legit", "sql": "SELECT 1",
             "required": [], "optional": [], "allow_writes": False},
        ]
        src._write_yamls(tmp_path, validations=[], queries=queries)
        assert (tmp_path / "queries" / "reports" / "legit.yaml").exists()
        assert "invalid segment" in caplog.text

    def test_query_path_absolute_rejected(self, tmp_path, caplog):
        src = self._src()
        queries = [
            {"path": "/etc/passwd", "sql": "SELECT 1",
             "required": [], "optional": [], "allow_writes": False},
        ]
        src._write_yamls(tmp_path, validations=[], queries=queries)
        assert "absolute" in caplog.text or "contains \\" in caplog.text

    def test_query_path_backslash_rejected(self, tmp_path, caplog):
        src = self._src()
        queries = [
            {"path": "reports\\evil", "sql": "SELECT 1",
             "required": [], "optional": [], "allow_writes": False},
        ]
        src._write_yamls(tmp_path, validations=[], queries=queries)
        assert "absolute" in caplog.text or "contains \\" in caplog.text

    def test_query_path_empty_segment_rejected(self, tmp_path, caplog):
        """`reports//by_id` (double slash) has an empty segment between
        the slashes — reject before it becomes ambiguous on the FS."""
        src = self._src()
        queries = [
            {"path": "reports//by_id", "sql": "SELECT 1",
             "required": [], "optional": [], "allow_writes": False},
        ]
        src._write_yamls(tmp_path, validations=[], queries=queries)
        assert "invalid segment" in caplog.text

    def test_query_path_nested_dot_segment_rejected(self, tmp_path, caplog):
        """`reports/./by_id` has a `.` segment — reject."""
        src = self._src()
        queries = [
            {"path": "reports/./by_id", "sql": "SELECT 1",
             "required": [], "optional": [], "allow_writes": False},
        ]
        src._write_yamls(tmp_path, validations=[], queries=queries)
        assert "invalid segment" in caplog.text

    def test_legitimate_rows_still_load(self, tmp_path):
        """Sanity — common shapes work unchanged."""
        src = self._src()
        validations = [
            {"schema_name": "public", "table_name": "orders",
             "required": ["id"], "optional": []},
            {"schema_name": "reporting_v2", "table_name": "sites_by_state",
             "required": [], "optional": ["region"]},
        ]
        queries = [
            {"path": "reports/by_id", "sql": "SELECT 1",
             "required": [], "optional": [], "allow_writes": False},
            {"path": "audit/events/list_recent", "sql": "SELECT 1",
             "required": [], "optional": [], "allow_writes": False},
        ]
        src._write_yamls(tmp_path, validations, queries)
        assert (tmp_path / "validation" / "public" / "orders.yaml").exists()
        assert (tmp_path / "validation" / "reporting_v2" / "sites_by_state.yaml").exists()
        assert (tmp_path / "queries" / "reports" / "by_id.yaml").exists()
        assert (tmp_path / "queries" / "audit" / "events" / "list_recent.yaml").exists()


# ---------------------------------------------------------------- real Postgres


_REAL_DSN = os.environ.get(
    "TEST_CONFIG_DSN",
    "host=localhost port=5432 user=postgres password=testpass dbname=postgres",
)


def _postgres_reachable() -> bool:
    """Probe the configured DSN; skip the real-Postgres tests when
    nothing answers. Lets the test suite run cleanly with or without
    test-infra up."""
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(_REAL_DSN, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone()[0] == 1
    except Exception:
        return False


class TestRealPostgres:
    """End-to-end format validation against a real Postgres. Uses a
    deployment-name unique to this test run so concurrent runs don't
    stomp on each other; cleans up after itself."""

    @pytest.fixture(autouse=True)
    def _skip_when_no_db(self):
        if not _postgres_reachable():
            pytest.skip(
                "Postgres not reachable at TEST_CONFIG_DSN; bring up "
                "test-infra/docker-compose.yml or skip this class."
            )

    @pytest.fixture
    def deployment_name(self):
        # Postgres identifiers are case-sensitive and must satisfy the
        # AppSettings deployment_name regex; use a prefix + short randomish suffix.
        import uuid
        name = f"app_test_{uuid.uuid4().hex[:8]}"
        yield name
        # Teardown: drop the database we created.
        import psycopg
        with psycopg.connect(_REAL_DSN, dbname="postgres", autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'DROP DATABASE IF EXISTS "{name}"',
                )

    def test_full_bootstrap_and_materialize_against_real_db(self, deployment_name):
        s = DbSource(dsn=_REAL_DSN, deployment_name=deployment_name)
        target = s.materialize()

        # First materialize: empty DB, empty target (no validation/ or
        # queries/ YAMLs to emit). The dirs themselves don't have to
        # exist yet — loaders gracefully handle missing dirs.
        assert target.exists()

    def test_round_trips_validation_row(self, deployment_name):
        s = DbSource(dsn=_REAL_DSN, deployment_name=deployment_name)
        s.materialize()  # bootstraps the schema

        # Insert a row directly via psycopg. Table name derives from
        # APP_NAME via DbSource.validations_table (#322) — default
        # app_name='app' yields 'app_config_validations'.
        import psycopg
        with psycopg.connect(_REAL_DSN, dbname=deployment_name) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {s.validations_table} "  # noqa: S608 — validated app_name prefix
                    "(schema_name, table_name, required, optional) "
                    "VALUES (%s, %s, %s::jsonb, %s::jsonb)",
                    ("public", "products", '["id"]', '["status"]'),
                )
                conn.commit()

        target = s.reload()
        f = target / "validation" / "public" / "products.yaml"
        assert f.exists()
        loaded = yaml.safe_load(f.read_text())
        assert loaded == {"params": {"required": ["id"], "optional": ["status"]}}

    def test_round_trips_query_row(self, deployment_name):
        s = DbSource(dsn=_REAL_DSN, deployment_name=deployment_name)
        s.materialize()

        import psycopg
        with psycopg.connect(_REAL_DSN, dbname=deployment_name) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {s.queries_table} "  # noqa: S608 — validated app_name prefix
                    "(path, sql, required, optional, allow_writes) "
                    "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s)",
                    ("reports/by_id", "SELECT * FROM t WHERE id = :id",
                     '["id"]', "[]", False),
                )
                conn.commit()

        target = s.reload()
        f = target / "queries" / "reports" / "by_id.yaml"
        assert f.exists()
        loaded = yaml.safe_load(f.read_text())
        assert loaded["sql"] == "SELECT * FROM t WHERE id = :id"
        assert loaded["params"] == {"required": ["id"], "optional": []}
