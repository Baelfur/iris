"""Shared pytest fixtures for variant integration tests.

Variants register this module as a pytest plugin via
``pytest_plugins = ["core.testing.fixtures"]`` in their
``tests/conftest.py``. Variant-specific fixtures (``schema_path``,
``products_path``, ``passthrough_creds_good``) stay local because they
differ per variant. Variants override ``client`` locally if they need
extra setup — Trino does this to seed its memory catalog before the
client comes up.

The ``app.main`` import inside ``client`` resolves against the variant's
own ``app/`` directory; pytest is invoked with cwd at the variant root
(``cd variants/X && python -m pytest tests/``), which puts ``app/`` on
sys.path. The fixture body runs in that cwd context, so the import
finds the variant's app, not core's.

YAML lookup path: this module sets ``CONFIG__LOCAL_ROOT=tests/fixtures``
at import time so the test client's lifespan finds custom queries +
view-def YAMLs in the per-variant ``tests/fixtures/`` tree. Production
deploys leave the variant root clean (no ``queries/`` or ``validation/``
directories) — the test fixtures live solely under ``tests/`` where
they belong.
"""

import os

# Set BEFORE any core import so the Settings model picks it up via
# pydantic-settings env-var resolution. Variants register this module
# via pytest_plugins; that import runs at conftest load time, well
# before the lifespan-triggering `client` fixture runs.
os.environ.setdefault("CONFIG__LOCAL_ROOT", "tests/fixtures")
# AUTH__MODE is required. Tests run in "gateway" mode — assume the
# caller has been validated upstream, just exercise service behavior.
os.environ.setdefault("AUTH__MODE", "gateway")
# AUTH__REQUIRE_PASSTHROUGH defaults true in production; tests
# default it false because the integration suite mostly exercises the
# pool-mode path. Tests that want to verify the require-passthrough
# enforcement opt back in via the ``require_passthrough`` fixture below.
os.environ.setdefault("AUTH__REQUIRE_PASSTHROUGH", "false")

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def client():
    """Async httpx client with LifespanManager around the variant's app.

    Imports ``app.main`` lazily so ``test_unit.py`` collection (which
    doesn't request ``client``) doesn't trigger the variant's
    ``Settings`` instantiation — useful when env vars aren't set.
    """
    from app.main import app
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    async with LifespanManager(app, shutdown_timeout=60):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def passthrough_creds_bad():
    """Wrong credentials — used to exercise the 401 path on passthrough.

    Variants that don't support passthrough (Trino in the test harness)
    override this fixture to return ``None``; integration tests skip
    when the fixture is None.
    """
    return ("nobody", "wrongpass")


@pytest.fixture
def require_passthrough(client):
    """Opt-in: flip ``auth.require_passthrough`` true for the test.

    The module-level env default keeps the integration suite running in
    pool-mode (false) so the bulk of tests exercise the service's query semantics
    without needing creds on every call. Tests for the require-
    passthrough enforcement opt in via this fixture; the depend on
    ``client`` ensures lifespan has populated the context first.
    """
    from core.context import get_context

    settings = get_context().settings
    original = settings.auth.require_passthrough
    settings.auth.require_passthrough = True
    yield
    settings.auth.require_passthrough = original
