"""Variant-specific unit tests for Oracle.

Paramstyle-specific SQL emission (named, with uppercased schema/table)
is covered once for all paramstyles in
``core/tests/test_paramstyle_emission.py``. Oracle has no additional
variant-specific build_query behavior beyond that, so this file holds
only a smoke check that the variant's ``app.db`` module imports
cleanly (the rest of the Oracle surface is exercised by
``test_integration.py`` against a live DB). (#200)
"""


def test_app_db_imports(monkeypatch):
    """Sanity: app.db loads when its Settings env vars are set."""
    monkeypatch.setenv("ORACLE_HOST", "x")
    monkeypatch.setenv("ORACLE_USER", "x")
    monkeypatch.setenv("ORACLE_PASSWORD", "x")
    monkeypatch.setenv("ORACLE_SERVICE", "x")
    monkeypatch.setenv("CONFIG__SOURCE", "local")
    from app import db

    assert hasattr(db, "harvest_ddl")
    assert hasattr(db, "fetch_all")
    assert hasattr(db, "ping")
