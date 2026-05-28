"""Integration test: service-account username redaction in route catch blocks.

The product claim in ``security-posture.md`` Control 8 / ``base_settings.py``
field comment says usernames are HMAC-redacted in error logs. Pre-#129
that was only true for the passthrough path (``X-DB-Authorization``);
the configured service-account username could appear unredacted when
driver text echoed it back. This test pins the post-fix behavior:
``ctx.db_user`` is always redacted when the route's DatabaseError
catch block fires, regardless of whether passthrough creds were
present.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.context import AppContext, set_context
from core.engine import schema_cache
from core.errors.exceptions import DatabaseError
from core.routes.inventory import router as inventory_router


@dataclass
class _Auth:
    mode: str = "gateway"
    jwks_url: str = ""
    audience: str = ""
    issuer: str = ""
    admin_token: str = ""
    require_passthrough: bool = False


@dataclass
class _CircuitBreaker:
    enabled: bool = False


@dataclass
class _Settings:
    deployment_name: str = ""
    max_page_size: int = 0
    error_detail: str = "terse"
    log_user_secret: str = ""
    auth: _Auth = field(default_factory=_Auth)
    circuit_breaker: _CircuitBreaker = field(default_factory=_CircuitBreaker)


def _make_app_with_failing_fetch(detail: str, db_user: str, log_user_secret: str = "") -> FastAPI:
    """Build a minimal FastAPI app whose AppContext has a fetch_all
    that always raises ``DatabaseError(detail)``."""

    async def raising_fetch(sql, params=None):
        raise DatabaseError(detail)

    set_context(AppContext(
        fetch_all=raising_fetch,
        harvest_ddl=None,
        paramstyle="pyformat",
        settings=_Settings(log_user_secret=log_user_secret),
        database="postgresql",
        db_user=db_user,
    ))

    # Seed the DDL cache so the route reaches fetch_all rather than
    # bouncing on the cache validation step.
    schema_cache._cache.clear()
    schema_cache._cache["public"] = {"products": {"id", "name"}}

    app = FastAPI()
    app.include_router(inventory_router)
    return app


class TestServiceAccountRedaction:
    """#129 — driver-error text mentioning the service-account username
    must be HMAC-redacted before logging or shipping to a Kafka sink."""

    def teardown_method(self):
        from core import context
        context._ctx = None
        schema_cache._cache.clear()

    def test_pool_path_redacts_service_account_user(self, caplog, monkeypatch):
        """No passthrough creds → driver text mentions service-account
        username → redacted via ctx.db_user."""
        # log_user_secret is now passed through Settings, not env

        leaky_detail = (
            'FATAL: permission denied for user "metadata_user" on table public.products'
        )
        app = _make_app_with_failing_fetch(leaky_detail, db_user="metadata_user", log_user_secret="test-secret")

        with caplog.at_level(logging.ERROR):
            resp = TestClient(app).get("/public/products?id=1")

        assert resp.status_code == 400
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert "metadata_user" not in log_text, (
            f"Service-account username leaked into logs: {log_text!r}"
        )
        assert "user:" in log_text  # HMAC-redacted form is present

    def test_passthrough_path_redacts_both(self, caplog, monkeypatch):
        """Passthrough creds present + driver text mentioning both the
        passthrough caller AND the service account → both redacted."""
        # log_user_secret is now passed through Settings, not env

        leaky_detail = (
            'FATAL: role "metadata_user" cannot impersonate caller "alice"'
        )
        app = _make_app_with_failing_fetch(leaky_detail, db_user="metadata_user", log_user_secret="test-secret")

        client = TestClient(app)
        # X-DB-Authorization Basic alice:wrongpass
        import base64
        creds = base64.b64encode(b"alice:wrongpass").decode()

        with caplog.at_level(logging.ERROR):
            resp = client.get(
                "/public/products?id=1",
                headers={"X-DB-Authorization": f"Basic {creds}"},
            )

        assert resp.status_code == 400
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert "metadata_user" not in log_text
        assert "alice" not in log_text

    def test_empty_db_user_is_safe(self, caplog, monkeypatch):
        """If a deployment somehow has db_user="", the catch block
        skips redaction without crashing — the existing redact_username
        empty-username noop test pins that primitive; this exercises
        the route-level guard."""
        # log_user_secret is now passed through Settings, not env

        app = _make_app_with_failing_fetch(
            "some driver error", db_user="", log_user_secret="test-secret",
        )
        with caplog.at_level(logging.ERROR):
            resp = TestClient(app).get("/public/products?id=1")

        assert resp.status_code == 400
