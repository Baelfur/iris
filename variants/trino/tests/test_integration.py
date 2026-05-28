"""Integration tests — run shared suite against a Trino coordinator with the memory catalog.

All tests come from :mod:`core.testing.integration_suite`; per-variant
context (URL paths, the ASGI client, the data seed) is provided by fixtures
in ``conftest.py``. Trino's passthrough fixtures return None so the
passthrough tests skip automatically.

The Trino-specific 3-segment route ``/{catalog}/{schema}/{view_name}`` is
not part of the shared suite (no other variant has catalogs), so it's
covered here directly. (#152)
"""

import pytest

from core.testing.integration_suite import *  # noqa: F401, F403


class TestThreeSegmentRoute:
    """Trino-only ``/{catalog}/{schema}/{view_name}`` URL shape. (#152)"""

    @pytest.mark.asyncio
    async def test_configured_catalog_returns_data(self, client):
        # The conftest seeds memory.public.products; the variant's
        # TRINO_CATALOG env is "memory", so this should resolve.
        resp = await client.get("/memory/public/products")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "products"
        assert len(body["elements"]) > 0

    @pytest.mark.asyncio
    async def test_configured_catalog_case_insensitive(self, client):
        resp = await client.get("/MEMORY/public/products")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unknown_catalog_404(self, client):
        resp = await client.get("/unknown_catalog/public/products")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_two_segment_route_still_works(self, client):
        # Backwards-compat: the existing 2-segment route uses the
        # connection-level default catalog and continues to work.
        resp = await client.get("/public/products")
        assert resp.status_code == 200
