"""Per-variant fixture overrides. See variants/postgres/tests/conftest.py
for the docstring; this file is the same shape with Oracle-specific
schema/credential values (Oracle's "schema" is the user, hence the
public_user path).
"""

import pytest

pytest_plugins = ["core.testing.fixtures"]


@pytest.fixture
def schema_path():
    return "/public_user"


@pytest.fixture
def products_path(schema_path):
    return f"{schema_path}/products"


@pytest.fixture
def passthrough_creds_good():
    return ("public_user", "testpass")
