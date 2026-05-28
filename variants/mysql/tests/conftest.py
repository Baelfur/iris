"""Per-variant fixture overrides. See variants/postgres/tests/conftest.py
for the docstring; this file is the same shape.
"""

import pytest

pytest_plugins = ["core.testing.fixtures"]


@pytest.fixture
def schema_path():
    return "/public"


@pytest.fixture
def products_path(schema_path):
    return f"{schema_path}/products"


@pytest.fixture
def passthrough_creds_good():
    return ("passthrough_user", "passpass")
