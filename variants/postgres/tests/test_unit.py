"""Variant-specific unit tests for Postgres.

Paramstyle-specific SQL emission (pyformat in this variant's case) is
covered once for all paramstyles in
``core/tests/test_paramstyle_emission.py``. This file holds Postgres-only
behavior — currently the harvest_ddl SQL emission against a mocked pool.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _import_db(monkeypatch):
    """Import ``app.db`` after stubbing the env vars its Settings demand."""
    monkeypatch.setenv("PG_HOST", "x")
    monkeypatch.setenv("PG_USER", "x")
    monkeypatch.setenv("PG_PASSWORD", "x")
    monkeypatch.setenv("PG_DATABASE", "x")
    monkeypatch.setenv("CONFIG__SOURCE", "local")
    from app import db

    return db


def _patch_pool(monkeypatch, captured: dict):
    """Wire a mock pool into ``app.db`` so harvest_ddl runs without a real DB.

    ``harvest_ddl`` issues two queries (columns + indexes); ``captured``
    accumulates a list under ``sqls`` / ``params`` keyed by call order.
    """
    captured.setdefault("sqls", [])
    captured.setdefault("params", [])
    cur = AsyncMock()
    cur.fetchall.return_value = []

    def execute(sql, params):
        captured["sqls"].append(sql)
        captured["params"].append(list(params))

    cur.execute.side_effect = execute

    cm_cur = MagicMock()
    cm_cur.__aenter__ = AsyncMock(return_value=cur)
    cm_cur.__aexit__ = AsyncMock(return_value=False)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cm_cur)

    cm_conn = MagicMock()
    cm_conn.__aenter__ = AsyncMock(return_value=conn)
    cm_conn.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.connection = MagicMock(return_value=cm_conn)

    db = _import_db(monkeypatch)
    monkeypatch.setattr(db, "pool", pool)
    return db


class TestHarvestDdlSql:
    """harvest_ddl emits a columns query (``information_schema.columns``)
    and an index query (``pg_index``-based join). Both exclude system
    schemas; DB role grants determine what's actually returned. The
    allowlist YAML (#269) narrows the cache post-harvest, not at SQL
    time.
    """

    def test_columns_query_excludes_system_schemas(self, monkeypatch):
        captured: dict = {}
        db = _patch_pool(monkeypatch, captured)

        asyncio.run(db.harvest_ddl())

        # First execute is the columns query; second is the index query.
        cols_sql, index_sql = captured["sqls"]
        cols_params, index_params = captured["params"]
        assert "information_schema.columns" in cols_sql
        assert "table_schema NOT IN" in cols_sql
        assert set(cols_params) == set(db.SYSTEM_SCHEMAS)
        # Index query also excludes system schemas (different column name).
        assert "pg_index" in index_sql
        assert "n.nspname NOT IN" in index_sql
        assert set(index_params) == set(db.SYSTEM_SCHEMAS)
