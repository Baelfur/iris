"""Variant-specific unit tests for MariaDB.

Paramstyle-specific SQL emission (pyformat) is covered once for all
paramstyles in ``core/tests/test_paramstyle_emission.py``. MariaDB has
no additional variant-specific build_query behavior beyond that, so
this file holds only a smoke check that the variant's ``app.db``
module imports cleanly (the rest of the MariaDB surface is exercised
by ``test_integration.py`` against a live DB). (#200)
"""


def test_app_db_imports(monkeypatch):
    """Sanity: app.db loads when its Settings env vars are set."""
    monkeypatch.setenv("MARIADB_HOST", "x")
    monkeypatch.setenv("MARIADB_USER", "x")
    monkeypatch.setenv("MARIADB_PASSWORD", "x")
    monkeypatch.setenv("MARIADB_DATABASE", "x")
    monkeypatch.setenv("CONFIG__SOURCE", "local")
    from app import db

    assert hasattr(db, "harvest_ddl")
    assert hasattr(db, "fetch_all")
    assert hasattr(db, "ping")
