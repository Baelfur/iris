"""Optional async circuit breaker for DB calls.

Off by default. Operators on flakier infrastructure (legacy DBs with
transient outages, network partitions) flip ``CIRCUIT_BREAKER_ENABLED``
so a sustained DB outage stops every incoming request from sitting on
its driver timeout — the breaker short-circuits subsequent calls with a
``503`` + ``Retry-After`` and lets the gateway back off.

State machine:

- **closed**: pass through; count consecutive failures
- **open**: short-circuit with ``CircuitBreakerOpen``; wait
  ``reset_timeout`` seconds before allowing a probe
- **half-open**: one probe request allowed; success → closed (counter
  reset); failure → open (timer restarts)

Per-process state. With multiple uvicorn workers the breakers are
independent — that's intentional. Each worker sheds load on its own
view of "is the DB up," and Kubernetes / the gateway sees the union.
A shared/distributed breaker would need an out-of-process state store
and is out of scope.

Hand-rolled rather than pulling in ``pybreaker`` because the surface
is small (one async ``call`` method) and we want zero new deps for an
opt-in feature.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitBreakerOpen(Exception):
    """Raised by ``CircuitBreaker.call`` when the breaker is open.

    ``retry_after`` is the integer seconds the caller should wait before
    retrying — surfaced via the HTTP ``Retry-After`` header so gateways
    back off intelligently rather than retrying immediately.
    """

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"circuit breaker open, retry after {retry_after}s")


class CircuitBreaker:
    """Async circuit breaker.

    ``fail_max`` consecutive failures inside ``call`` flip the breaker
    open. While open, ``call`` raises ``CircuitBreakerOpen`` immediately
    without invoking the wrapped function. After ``reset_timeout``
    seconds the breaker enters half-open: the next ``call`` is allowed
    through; success resets to closed, failure re-opens.
    """

    def __init__(self, fail_max: int = 5, reset_timeout: float = 5.0):
        if fail_max < 1:
            raise ValueError("fail_max must be >= 1")
        if reset_timeout <= 0:
            raise ValueError("reset_timeout must be > 0")
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self._lock = asyncio.Lock()
        self._failures = 0
        # None → closed; set → open or half-open depending on elapsed time.
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        """Current state — `closed`, `open`, or `half_open`."""
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at < self.reset_timeout:
            return "open"
        return "half_open"

    def _retry_after_seconds(self) -> int:
        """Seconds remaining until the breaker enters half-open. Floors at 1
        so the ``Retry-After`` header is never zero — clients shouldn't
        hammer-retry instantly."""
        if self._opened_at is None:
            return 1
        remaining = self.reset_timeout - (time.monotonic() - self._opened_at)
        return max(1, int(remaining))

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn()`` through the breaker.

        Raises :class:`CircuitBreakerOpen` when short-circuiting.
        Re-raises anything ``fn`` raises (after recording the failure).
        """
        if self.state == "open":
            raise CircuitBreakerOpen(retry_after=self._retry_after_seconds())

        try:
            result = await fn()
        except Exception:
            async with self._lock:
                self._failures += 1
                if self._failures >= self.fail_max:
                    if self._opened_at is None:
                        logger.warning(
                            "Circuit breaker tripped after %d consecutive failures; "
                            "shedding load for %.1fs",
                            self._failures,
                            self.reset_timeout,
                        )
                    self._opened_at = time.monotonic()
            raise

        # Success — close (in case we were half-open) and reset the counter.
        async with self._lock:
            if self._opened_at is not None:
                logger.info("Circuit breaker closed after successful probe")
            self._failures = 0
            self._opened_at = None
        return result


def from_settings(settings) -> Optional["CircuitBreaker"]:
    """Construct a CircuitBreaker from settings, or return None when the
    feature is off. Variants call this in lifespan startup to populate
    ``AppContext.breaker``.
    """
    if not settings.circuit_breaker.enabled:
        return None
    return CircuitBreaker(
        fail_max=settings.circuit_breaker.fail_max,
        reset_timeout=settings.circuit_breaker.reset_timeout,
    )


async def fetch_with_breaker(ctx, sql, binds, creds=None):
    """Run ``fetch_all`` (or ``fetch_all_with_creds`` when creds given)
    through the context's optional breaker.

    Routes call this instead of ``ctx.fetch_all`` so breaker handling
    lives in one place. Raises :class:`CircuitBreakerOpen` when shedding
    load; raises :class:`core.exceptions.DatabaseError` on
    driver errors.
    """

    async def _fetch():
        if creds and ctx.fetch_all_with_creds:
            return await ctx.fetch_all_with_creds(sql, binds or None, creds)
        return await ctx.fetch_all(sql, binds or None)

    if ctx.breaker is not None:
        return await ctx.breaker.call(_fetch)
    return await _fetch()
