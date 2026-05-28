"""Tests for core.routes.readiness._probe.

Uses a stub AppContext so no real DB is required. Paramstyle is irrelevant
here — readiness only calls ctx.ping, not the query engine.
"""

import asyncio

import pytest
from fastapi import HTTPException

from core.context import AppContext, set_context
from core.routes.readiness import _probe


def _make_ctx(ping=None, timeout_ms=500):
    class _S:
        pass
    s = _S()
    s.readiness_timeout_ms = timeout_ms
    s.deployment_name = ""

    async def noop(*a, **k):
        return []

    async def noop_h():
        return {}

    set_context(AppContext(
        fetch_all=noop, harvest_ddl=noop_h, paramstyle="pyformat",
        settings=s, database="test", ping=ping,
    ))


def test_ready_when_ping_succeeds():
    async def ok():
        return None

    _make_ctx(ping=ok, timeout_ms=500)
    assert asyncio.run(_probe()) == {"status": "ready"}


def test_503_when_ping_times_out():
    async def slow():
        await asyncio.sleep(1)

    _make_ctx(ping=slow, timeout_ms=50)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_probe())
    assert exc_info.value.status_code == 503
    assert "timed out" in exc_info.value.detail["reason"]


def test_503_when_ping_raises():
    async def boom():
        raise RuntimeError("db blew up")

    _make_ctx(ping=boom, timeout_ms=500)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(_probe())
    assert exc_info.value.status_code == 503
    assert "unreachable" in exc_info.value.detail["reason"]


def test_disabled_when_timeout_zero():
    """READINESS_TIMEOUT_MS=0 disables the DB hit; endpoint still returns ready."""
    pinged = []
    async def should_not_be_called():
        pinged.append(1)

    _make_ctx(ping=should_not_be_called, timeout_ms=0)
    assert asyncio.run(_probe()) == {"status": "ready"}
    assert pinged == []


def test_disabled_when_timeout_negative():
    """Negative treated as disabled (forgiving input)."""
    _make_ctx(ping=None, timeout_ms=-1)
    assert asyncio.run(_probe()) == {"status": "ready"}


def test_ready_when_ping_not_wired():
    """No ping fn on context ⇒ endpoint still returns ready."""
    _make_ctx(ping=None, timeout_ms=500)
    assert asyncio.run(_probe()) == {"status": "ready"}
