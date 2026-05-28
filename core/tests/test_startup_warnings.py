"""Tests for core.startup_warnings.report and report_post_harvest.

Each variant calls these at lifespan startup. ``report`` emits WARNINGs
for unsafe defaults pre-harvest (#131); ``report_post_harvest`` emits
INFOs for state that depends on the DDL cache being populated (#135).
Well-configured deployments get no-op calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core import startup_warnings
from core.engine import schema_cache


@dataclass
class _Auth:
    mode: str = "gateway"
    require_passthrough: bool = True


@dataclass
class _Settings:
    max_page_size: int = 10000
    allowlist_mode: str = "enforce"
    auth: _Auth = field(default_factory=_Auth)


class TestStartupWarnings:
    def test_warns_on_max_page_size_zero(self, caplog):
        with caplog.at_level(logging.WARNING):
            startup_warnings.report(_Settings(max_page_size=0))

        msgs = [r.getMessage() for r in caplog.records]
        assert any("MAX_PAGE_SIZE" in m for m in msgs), \
            f"expected MAX_PAGE_SIZE warning, got: {msgs!r}"
        assert any("unbounded" in m for m in msgs)

    def test_no_warning_when_max_page_size_is_set(self, caplog):
        with caplog.at_level(logging.WARNING):
            startup_warnings.report(_Settings(max_page_size=1000))

        msgs = [r.getMessage() for r in caplog.records]
        assert not any("MAX_PAGE_SIZE" in m for m in msgs), \
            f"unexpected warning when set: {msgs!r}"

    def test_warns_on_auth_mode_open(self, caplog):
        with caplog.at_level(logging.WARNING):
            startup_warnings.report(_Settings(auth=_Auth(mode="open")))

        msgs = [r.getMessage() for r in caplog.records]
        assert any("AUTH__MODE=open" in m for m in msgs), \
            f"expected open-mode warning, got: {msgs!r}"

    def test_no_warning_when_mode_gateway_or_jwt(self, caplog):
        for mode in ("gateway", "jwt"):
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                startup_warnings.report(_Settings(auth=_Auth(mode=mode)))
            msgs = [r.getMessage() for r in caplog.records]
            assert not any("AUTH__MODE=open" in m for m in msgs), \
                f"unexpected open-mode warning at mode={mode}: {msgs!r}"

    def test_warns_on_require_passthrough_false(self, caplog):
        with caplog.at_level(logging.WARNING):
            startup_warnings.report(
                _Settings(auth=_Auth(mode="gateway", require_passthrough=False))
            )

        msgs = [r.getMessage() for r in caplog.records]
        assert any("AUTH__REQUIRE_PASSTHROUGH=false" in m for m in msgs), \
            f"expected require-passthrough-off warning, got: {msgs!r}"

    def test_warns_on_allowlist_presentation_mode(self, caplog):
        with caplog.at_level(logging.WARNING):
            startup_warnings.report(_Settings(allowlist_mode="presentation"))

        msgs = [r.getMessage() for r in caplog.records]
        assert any("ALLOWLIST__MODE=presentation" in m for m in msgs), \
            f"expected presentation-mode warning, got: {msgs!r}"
        assert any("NOT a security boundary" in m for m in msgs)

    def test_no_warning_when_allowlist_enforce(self, caplog):
        with caplog.at_level(logging.WARNING):
            startup_warnings.report(_Settings(allowlist_mode="enforce"))
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("ALLOWLIST__MODE=presentation" in m for m in msgs), \
            f"unexpected warning at enforce mode: {msgs!r}"

class TestReportPostHarvest:
    """``report_post_harvest`` surfaces grants-driven dynamic surface size
    when no allowlist.yaml is supplied. (#269)"""

    def setup_method(self):
        from core.loaders import allowlist
        schema_cache._cache.clear()
        schema_cache._cache["public"] = {
            "users": {"id", "name"},
            "orders": {"id", "user_id"},
        }
        schema_cache._cache["audit"] = {"events": {"id", "ts"}}
        allowlist._loaded = allowlist.Allowlist()

    def teardown_method(self):
        from core.loaders import allowlist
        schema_cache._cache.clear()
        allowlist._loaded = allowlist.Allowlist()

    def test_logs_when_allowlist_empty(self, caplog):
        with caplog.at_level(logging.INFO):
            startup_warnings.report_post_harvest(_Settings())

        msgs = [r.getMessage() for r in caplog.records]
        info = next((m for m in msgs if "No allowlist.yaml" in m), None)
        assert info is not None, f"expected info log, got: {msgs!r}"
        assert "2 schema(s)" in info
        assert "3 table(s)" in info

    def test_silent_when_allowlist_set(self, caplog):
        from core.loaders import allowlist
        allowlist._loaded = allowlist.Allowlist(schemas=["public"])
        with caplog.at_level(logging.INFO):
            startup_warnings.report_post_harvest(_Settings())

        msgs = [r.getMessage() for r in caplog.records]
        assert not any("No allowlist.yaml" in m for m in msgs), \
            f"unexpected log when set: {msgs!r}"

