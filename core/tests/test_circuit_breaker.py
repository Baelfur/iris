"""Tests for core.circuit_breaker.

Async state machine, hand-rolled to avoid the pybreaker dep. Verify all
three transitions (closed → open → half-open → closed/open) plus the
edge cases that bite in practice: counter reset on success, timer
restart on half-open failure, settings ↔ instance plumbing.
"""

import asyncio
from types import SimpleNamespace

import pytest

from core.engine.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpen,
    fetch_with_breaker,
    from_settings,
)


def _settings(**kwargs):
    """Build a stand-in settings object with the circuit_breaker submodel populated."""
    defaults = {
        "enabled": True,
        "fail_max": 3,
        "reset_timeout": 0.1,
    }
    # Allow tests to pass either nested-style (enabled=False) or
    # legacy-flat (circuit_breaker_enabled=False) names; tests below
    # use both shapes during the transition.
    nested_keys = {
        "circuit_breaker_enabled": "enabled",
        "circuit_breaker_fail_max": "fail_max",
        "circuit_breaker_reset_timeout": "reset_timeout",
    }
    for legacy, nested in nested_keys.items():
        if legacy in kwargs:
            kwargs[nested] = kwargs.pop(legacy)
    defaults.update(kwargs)
    return SimpleNamespace(circuit_breaker=SimpleNamespace(**defaults))


class TestStateTransitions:
    @pytest.mark.asyncio
    async def test_starts_closed(self):
        b = CircuitBreaker()
        assert b.state == "closed"

    @pytest.mark.asyncio
    async def test_passes_through_when_closed(self):
        b = CircuitBreaker()

        async def ok():
            return "value"

        assert await b.call(ok) == "value"
        assert b.state == "closed"

    @pytest.mark.asyncio
    async def test_opens_after_fail_max_consecutive_failures(self):
        b = CircuitBreaker(fail_max=3, reset_timeout=10)

        async def boom():
            raise RuntimeError("db down")

        # First fail_max-1 failures pass through and re-raise but stay closed.
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await b.call(boom)
            assert b.state == "closed"

        # The fail_max-th failure trips the breaker.
        with pytest.raises(RuntimeError):
            await b.call(boom)
        assert b.state == "open"

    @pytest.mark.asyncio
    async def test_short_circuits_when_open(self):
        b = CircuitBreaker(fail_max=1, reset_timeout=10)

        async def boom():
            raise RuntimeError("db down")

        with pytest.raises(RuntimeError):
            await b.call(boom)
        assert b.state == "open"

        called = []

        async def should_not_run():
            called.append(1)
            return "nope"

        with pytest.raises(CircuitBreakerOpen) as exc:
            await b.call(should_not_run)
        assert called == [], "open breaker should short-circuit before calling"
        assert exc.value.retry_after >= 1

    @pytest.mark.asyncio
    async def test_half_open_after_reset_timeout(self):
        b = CircuitBreaker(fail_max=1, reset_timeout=0.05)

        async def boom():
            raise RuntimeError("db down")

        with pytest.raises(RuntimeError):
            await b.call(boom)
        assert b.state == "open"

        await asyncio.sleep(0.07)
        assert b.state == "half_open"

    @pytest.mark.asyncio
    async def test_half_open_success_closes_breaker(self):
        b = CircuitBreaker(fail_max=1, reset_timeout=0.05)

        async def boom():
            raise RuntimeError("db down")

        async def ok():
            return "recovered"

        with pytest.raises(RuntimeError):
            await b.call(boom)
        await asyncio.sleep(0.07)
        assert b.state == "half_open"

        result = await b.call(ok)
        assert result == "recovered"
        assert b.state == "closed"

    @pytest.mark.asyncio
    async def test_half_open_failure_re_opens(self):
        b = CircuitBreaker(fail_max=1, reset_timeout=0.05)

        async def boom():
            raise RuntimeError("db down")

        with pytest.raises(RuntimeError):
            await b.call(boom)
        await asyncio.sleep(0.07)
        assert b.state == "half_open"

        with pytest.raises(RuntimeError):
            await b.call(boom)
        assert b.state == "open"

    @pytest.mark.asyncio
    async def test_success_in_closed_state_resets_failure_counter(self):
        """Two failures, then a success, should reset the counter so the
        next two failures don't trip the breaker."""
        b = CircuitBreaker(fail_max=3, reset_timeout=10)

        async def boom():
            raise RuntimeError("blip")

        async def ok():
            return "fine"

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await b.call(boom)
        assert b.state == "closed"

        await b.call(ok)  # resets counter

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await b.call(boom)
        assert b.state == "closed", \
            "counter should have reset; not yet at fail_max"


class TestRetryAfterHeader:
    @pytest.mark.asyncio
    async def test_retry_after_floors_at_one_second(self):
        """Even if elapsed time is just shy of reset_timeout, the
        Retry-After value should never be 0 — clients shouldn't
        hammer-retry instantly."""
        b = CircuitBreaker(fail_max=1, reset_timeout=0.5)

        async def boom():
            raise RuntimeError("blip")

        with pytest.raises(RuntimeError):
            await b.call(boom)

        # Sleep most of the way through the timeout.
        await asyncio.sleep(0.4)
        with pytest.raises(CircuitBreakerOpen) as exc:
            await b.call(boom)
        assert exc.value.retry_after >= 1


class TestFromSettings:
    def test_disabled_returns_none(self):
        s = _settings(circuit_breaker_enabled=False)
        assert from_settings(s) is None

    def test_enabled_constructs_with_settings(self):
        s = _settings(
            circuit_breaker_enabled=True,
            circuit_breaker_fail_max=7,
            circuit_breaker_reset_timeout=12.5,
        )
        b = from_settings(s)
        assert b is not None
        assert b.fail_max == 7
        assert b.reset_timeout == 12.5


class TestFetchWithBreaker:
    """Verify the route-side helper threads breaker / no-breaker / creds
    paths correctly. Uses a SimpleNamespace ctx — no real DB."""

    @pytest.mark.asyncio
    async def test_no_breaker_calls_fetch_all(self):
        called = []

        async def fetch_all(sql, binds):
            called.append(("plain", sql, binds))
            return ["row"]

        ctx = SimpleNamespace(
            fetch_all=fetch_all, fetch_all_with_creds=None, breaker=None,
        )
        result = await fetch_with_breaker(ctx, "SELECT 1", None)
        assert result == ["row"]
        assert called == [("plain", "SELECT 1", None)]

    @pytest.mark.asyncio
    async def test_creds_route_to_fetch_all_with_creds(self):
        called = []

        async def fetch_all(sql, binds):
            called.append(("plain",))
            return []

        async def fetch_all_with_creds(sql, binds, creds):
            called.append(("with_creds", creds))
            return ["passthrough_row"]

        ctx = SimpleNamespace(
            fetch_all=fetch_all,
            fetch_all_with_creds=fetch_all_with_creds,
            breaker=None,
        )
        result = await fetch_with_breaker(ctx, "SELECT 1", None, ("u", "p"))
        assert result == ["passthrough_row"]
        assert called == [("with_creds", ("u", "p"))]

    @pytest.mark.asyncio
    async def test_breaker_open_short_circuits_before_fetch(self):
        called = []

        async def fetch_all(sql, binds):
            called.append("ran")
            return []

        b = CircuitBreaker(fail_max=1, reset_timeout=10)

        async def boom():
            raise RuntimeError("trip")

        with pytest.raises(RuntimeError):
            await b.call(boom)
        assert b.state == "open"

        ctx = SimpleNamespace(
            fetch_all=fetch_all, fetch_all_with_creds=None, breaker=b,
        )
        with pytest.raises(CircuitBreakerOpen):
            await fetch_with_breaker(ctx, "SELECT 1", None)
        assert called == [], \
            "open breaker should not invoke fetch_all"
