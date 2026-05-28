"""Shared fixtures for core tests."""

import pytest


@pytest.fixture
def ddl_cache():
    """Populated DDL cache with a representative schema.

    Tests that need DDL validation can use this fixture; the cache is cleared
    before and after the test, so test order does not matter.
    """
    import core.engine.schema_cache as sc
    sc._cache.clear()
    sc._cache["public"] = {
        "products": {"id", "name", "category", "price", "status"},
        "t": {"id", "name", "category", "price", "status", "city", "x"},
    }
    yield sc._cache
    sc._cache.clear()


@pytest.fixture
def ident_validator(ddl_cache):
    """Validator closure that accepts columns of `public.t` (a superset table)."""
    valid = ddl_cache["public"]["t"]
    return lambda col: None if col in valid else f"Invalid column(s): {col}"
