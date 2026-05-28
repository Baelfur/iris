"""Tests for the admin auth lane.

Two paths gated by ``verify_admin_access``:

- ``X-Admin-Token`` — shared static secret (the original path). Fail-closed
  when ``AUTH__ADMIN_TOKEN`` is unset.
- Bearer JWT with admin claim — added in #303. Fail-closed when
  ``AUTH__ADMIN_GROUP`` is unset.

Pins:
- Token-only behavior preserved bit-for-bit (legacy tests below)
- Claim shape matrix (list / string / scope) handled correctly
- Dispatcher routes to the right path; token wins when both are present
- Missing-claim case returns 403 (authenticated but unauthorized)
"""

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

from core import context
from core.auth import admin as admin_mod
from core.auth.admin import (
    _claim_has_admin_group,
    verify_admin_access,
    verify_admin_token,
    verify_admin_via_jwt,
)


@dataclass
class _Auth:
    admin_token: str = ""
    admin_group: str = ""
    admin_claim_name: str = "groups"
    jwks_url: str = ""
    audience: str = ""
    issuer: str = ""


@dataclass
class _Settings:
    auth: _Auth = field(default_factory=_Auth)
    # error_msg() reads error_detail to choose verbose-vs-terse rendering.
    # Default "terse" matches production posture.
    error_detail: str = "terse"


def _ctx(**auth_kwargs) -> context.AppContext:
    return context.AppContext(
        fetch_all=None, harvest_ddl=None,
        paramstyle="pyformat", settings=_Settings(auth=_Auth(**auth_kwargs)),
        database="test",
    )


def _request(headers: dict) -> Request:
    """Build a starlette Request with arbitrary headers (no body)."""
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/admin/x",
                    "headers": raw, "query_string": b""})


class TestVerifyAdminToken:
    def teardown_method(self):
        context._ctx = None

    def test_unconfigured_token_rejects_with_explicit_message(self):
        """The verbose-mode detail makes the misconfiguration explicit so
        operators tailing logs see ``not configured`` rather than just
        ``Unauthorized``."""
        ctx = _ctx(admin_token="")
        ctx.settings.error_detail = "verbose"
        context.set_context(ctx)  # noqa: E501
        with pytest.raises(HTTPException) as exc:
            verify_admin_token(_request({"X-Admin-Token": "anything"}))
        assert exc.value.status_code == 401
        assert "not configured" in exc.value.detail

    def test_missing_header_rejected(self):
        context.set_context(_ctx(admin_token="secret"))
        with pytest.raises(HTTPException) as exc:
            verify_admin_token(_request({}))
        assert exc.value.status_code == 401

    def test_wrong_token_rejected(self):
        context.set_context(_ctx(admin_token="secret"))
        with pytest.raises(HTTPException) as exc:
            verify_admin_token(_request({"X-Admin-Token": "nope"}))
        assert exc.value.status_code == 401

    def test_correct_token_accepted(self):
        context.set_context(_ctx(admin_token="secret"))
        # Returns None on success (no exception).
        assert verify_admin_token(_request({"X-Admin-Token": "secret"})) is None

    def test_header_lookup_is_case_insensitive(self):
        """HTTP headers are case-insensitive; starlette normalizes on read."""
        context.set_context(_ctx(admin_token="secret"))
        assert verify_admin_token(_request({"x-admin-token": "secret"})) is None


class TestClaimMatching:
    """``_claim_has_admin_group`` recognizes the three claim shapes
    real-world IDPs emit (#303 design call: list / single string /
    space-delimited scope)."""

    def test_list_claim_matches_when_group_present(self):
        payload = {"groups": ["engineering", "iris-admin", "users"]}
        assert _claim_has_admin_group(payload, "groups", "iris-admin")

    def test_list_claim_misses_when_group_absent(self):
        payload = {"groups": ["engineering", "users"]}
        assert not _claim_has_admin_group(payload, "groups", "iris-admin")

    def test_string_claim_exact_match(self):
        payload = {"role": "iris-admin"}
        assert _claim_has_admin_group(payload, "role", "iris-admin")

    def test_string_claim_no_substring_match(self):
        """A `role: iris-admin-readonly` claim must not satisfy
        `admin_group=iris-admin` — exact match only for plain strings."""
        payload = {"role": "iris-admin-readonly"}
        assert not _claim_has_admin_group(payload, "role", "iris-admin")

    def test_scope_claim_space_split_match(self):
        """OAuth ``scope`` claim is space-delimited; match against the
        token list, not substring."""
        payload = {"scope": "read write iris-admin profile"}
        assert _claim_has_admin_group(payload, "scope", "iris-admin")

    def test_scope_claim_no_substring_match(self):
        """`scope: "read write iris-admin-lite"` must not match
        `iris-admin` — splitting on whitespace prevents substring leaks."""
        payload = {"scope": "read write iris-admin-lite"}
        assert not _claim_has_admin_group(payload, "scope", "iris-admin")

    def test_missing_claim_returns_false(self):
        payload = {"sub": "alice", "iss": "https://idp.example.com"}
        assert not _claim_has_admin_group(payload, "groups", "iris-admin")


# --- JWT-path fixtures (modeled on test_auth_user.py) -----------------------


@pytest.fixture
def rsa_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def admin_jwt_context(monkeypatch, rsa_keypair):
    """Wire the admin JWT path with a faked JWKS client returning the
    test RSA public key. ``admin_group=iris-admin`` and
    ``admin_claim_name=groups`` matches the conventional shape;
    individual tests can override.
    """
    from core.errors import messages as error_messages

    _, public_pem = rsa_keypair

    fake_settings = SimpleNamespace(
        auth=SimpleNamespace(
            mode="jwt",
            jwks_url="https://idp.example.com/jwks",
            audience="",
            issuer="",
            admin_token="",
            admin_group="iris-admin",
            admin_claim_name="groups",
        ),
        error_detail="terse",
        deployment_name="",
        database="",
    )
    fake_ctx = SimpleNamespace(settings=fake_settings, database="postgresql")
    monkeypatch.setattr(admin_mod, "get_context", lambda: fake_ctx)
    monkeypatch.setattr(error_messages, "get_context", lambda: fake_ctx)

    fake_signing_key = SimpleNamespace(key=public_pem)
    fake_jwks = MagicMock()
    fake_jwks.get_signing_key_from_jwt = MagicMock(return_value=fake_signing_key)
    admin_mod._admin_jwks_client = fake_jwks
    yield fake_settings
    admin_mod._admin_jwks_client = None


def _bearer_request(token: str) -> Request:
    return _request({"Authorization": f"Bearer {token}"})


class TestVerifyAdminViaJwt:
    """JWT admin path — fail-closed when settings are unset, 403 when
    authenticated-but-no-admin-claim, 200-equivalent (returns payload)
    when the claim matches."""

    @pytest.mark.asyncio
    async def test_unconfigured_admin_group_rejects(self, admin_jwt_context):
        admin_jwt_context.auth.admin_group = ""
        with pytest.raises(HTTPException) as exc:
            await verify_admin_via_jwt(_bearer_request("any.token.here"))
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_bearer_rejected(self, admin_jwt_context):
        with pytest.raises(HTTPException) as exc:
            await verify_admin_via_jwt(_request({}))
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_jwt_with_admin_claim_accepted(self, admin_jwt_context, rsa_keypair):
        private_pem, _ = rsa_keypair
        good = jwt.encode(
            {"sub": "alice@example.com", "groups": ["users", "iris-admin"]},
            private_pem,
            algorithm="RS256",
        )
        payload = await verify_admin_via_jwt(_bearer_request(good))
        assert payload["sub"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_valid_jwt_missing_admin_claim_returns_403(
        self, admin_jwt_context, rsa_keypair
    ):
        """Authenticated but not authorized — distinguishes "wrong creds"
        from "you lack admin." Body is generic; specifics live in WARN log."""
        private_pem, _ = rsa_keypair
        no_admin = jwt.encode(
            {"sub": "bob@example.com", "groups": ["users", "engineering"]},
            private_pem,
            algorithm="RS256",
        )
        with pytest.raises(HTTPException) as exc:
            await verify_admin_via_jwt(_bearer_request(no_admin))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_expired_jwt_rejected_with_401(self, admin_jwt_context, rsa_keypair):
        import time

        private_pem, _ = rsa_keypair
        expired = jwt.encode(
            {"sub": "alice", "groups": ["iris-admin"], "exp": int(time.time()) - 60},
            private_pem,
            algorithm="RS256",
        )
        with pytest.raises(HTTPException) as exc:
            await verify_admin_via_jwt(_bearer_request(expired))
        assert exc.value.status_code == 401


class TestVerifyAdminAccessDispatcher:
    """The dispatcher routes to whichever path the request supplies
    headers for. Token wins when both are present."""

    def teardown_method(self):
        context._ctx = None

    @pytest.mark.asyncio
    async def test_no_headers_at_all_rejects(self):
        context.set_context(_ctx(admin_token="secret"))
        with pytest.raises(HTTPException) as exc:
            await verify_admin_access(_request({}))
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_token_path_when_only_token_present(self):
        context.set_context(_ctx(admin_token="secret"))
        # No exception → token path validated successfully.
        assert await verify_admin_access(_request({"X-Admin-Token": "secret"})) is None

    @pytest.mark.asyncio
    async def test_token_wins_when_both_headers_present(self, admin_jwt_context):
        """A gateway-injected admin token alongside a user-supplied JWT
        — token authoritative, JWT path not invoked."""
        admin_jwt_context.auth.admin_token = "secret"
        # JWT in the request is malformed; if the dispatcher fell through
        # to the JWT path, this would raise 401. Token-wins means no raise.
        assert await verify_admin_access(
            _request({"X-Admin-Token": "secret", "Authorization": "Bearer garbage"})
        ) is None

    @pytest.mark.asyncio
    async def test_jwt_path_when_only_bearer_present(self, admin_jwt_context, rsa_keypair):
        private_pem, _ = rsa_keypair
        good = jwt.encode(
            {"sub": "alice", "groups": ["iris-admin"]},
            private_pem,
            algorithm="RS256",
        )
        # Returns None when the JWT path accepts (dispatcher swallows the payload).
        assert await verify_admin_access(_bearer_request(good)) is None
