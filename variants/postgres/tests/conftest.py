"""Per-variant fixture overrides.

The ``client`` and ``passthrough_creds_bad`` fixtures come from
``core.testing.fixtures`` (registered as a plugin below). This file
holds only the fixtures that genuinely differ per variant: the URL
schema and the passthrough credentials known to the test harness.
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
