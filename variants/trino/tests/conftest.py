"""Per-variant fixture overrides — Trino flavor.

Trino's memory catalog starts empty in the test harness, so this module
seeds ``memory.public.products`` once per test module via an autouse-on-
``client``-dependency fixture, then overrides the shared ``client`` to
take that dependency. The shared ``client`` from
``core.testing.fixtures`` is registered via ``pytest_plugins`` but
shadowed by the local override below.

Trino doesn't expose passthrough credentials in this harness because
HTTP Basic over plain HTTP is rejected by Trino — the integration tests
that need them skip when the fixtures return ``None``.
"""

import os

import pytest
import pytest_asyncio

pytest_plugins = ["core.testing.fixtures"]

SEED_SQL = [
    "CREATE SCHEMA IF NOT EXISTS memory.public",
    "DROP TABLE IF EXISTS memory.public.products",
    """
    CREATE TABLE memory.public.products (
        id INTEGER,
        name VARCHAR,
        category VARCHAR,
        price DOUBLE,
        in_stock INTEGER
    )
    """,
    "INSERT INTO memory.public.products VALUES (1, 'Laptop', 'electronics', 999.99, 1)",
    "INSERT INTO memory.public.products VALUES (2, 'T-Shirt', 'clothing', 19.99, 1)",
    "INSERT INTO memory.public.products VALUES (3, 'Headphones', 'electronics', 149.99, 1)",
    "INSERT INTO memory.public.products VALUES (4, 'Jeans', 'clothing', 49.99, 0)",
    "INSERT INTO memory.public.products VALUES (5, 'Keyboard', 'electronics', 79.99, 1)",
    "INSERT INTO memory.public.products VALUES (6, 'Sneakers', 'clothing', 89.99, 1)",
]


@pytest_asyncio.fixture(scope="module")
async def _seed_trino():
    """Seed the memory catalog once per test module before any integration test runs."""
    from aiotrino.dbapi import Connection

    conn = Connection(
        host=os.environ.get("TRINO_HOST", "localhost"),
        port=int(os.environ.get("TRINO_PORT", "8080")),
        user=os.environ.get("TRINO_USER", "app"),
        catalog="memory",
    )
    try:
        cur = await conn.cursor()
        for stmt in SEED_SQL:
            await cur.execute(stmt)
            await cur.fetchall()
    finally:
        await conn.close()
    yield


@pytest_asyncio.fixture
async def client(_seed_trino):
    """Override the shared ``client`` to depend on ``_seed_trino`` so the
    memory catalog is populated before any request hits the app. The
    dependency is lazy — unit tests don't request ``client``, so
    ``_seed_trino`` never runs during a unit-test run.
    """
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with LifespanManager(app, shutdown_timeout=60):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def schema_path():
    return "/public"


@pytest.fixture
def products_path(schema_path):
    return f"{schema_path}/products"


@pytest.fixture
def passthrough_creds_good():
    """Trino passthrough requires HTTPS and isn't configured in the test harness."""
    return None


@pytest.fixture
def passthrough_creds_bad():
    return None
