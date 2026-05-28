"""Tests for core.creds.extract_basic_creds and core.redact.hash_username / redact_username."""

import base64
from unittest.mock import MagicMock

from core.auth.creds import extract_basic_creds
from core.redact import hash_username, redact_username


def _request(headers=None):
    req = MagicMock()
    req.headers = headers or {}
    return req


class TestExtractBasicCreds:
    def test_returns_tuple_for_valid_basic_auth(self):
        encoded = base64.b64encode(b"alice:secret123").decode()
        req = _request({"x-db-authorization": f"Basic {encoded}"})
        assert extract_basic_creds(req) == ("alice", "secret123")

    def test_returns_none_when_no_auth_header(self):
        assert extract_basic_creds(_request({})) is None

    def test_returns_none_for_bearer_token(self):
        req = _request({"authorization": "Bearer eyJhbGciOi..."})
        assert extract_basic_creds(req) is None

    def test_handles_password_with_colon(self):
        encoded = base64.b64encode(b"user:pass:with:colons").decode()
        req = _request({"x-db-authorization": f"Basic {encoded}"})
        assert extract_basic_creds(req) == ("user", "pass:with:colons")

    def test_returns_none_for_malformed_base64(self):
        req = _request({"x-db-authorization": "Basic !!!notbase64!!!"})
        assert extract_basic_creds(req) is None

    def test_case_insensitive_basic_prefix(self):
        encoded = base64.b64encode(b"user:pass").decode()
        req = _request({"x-db-authorization": f"basic {encoded}"})
        assert extract_basic_creds(req) == ("user", "pass")

    def test_ignores_bearer_on_authorization_header(self):
        """JWT on Authorization and Basic on X-DB-Authorization must coexist."""
        encoded = base64.b64encode(b"alice:secret").decode()
        req = _request({
            "authorization": "Bearer eyJhbGciOi...",
            "x-db-authorization": f"Basic {encoded}",
        })
        assert extract_basic_creds(req) == ("alice", "secret")

    def test_falls_back_to_standard_authorization_basic(self):
        """When X-DB-Authorization is absent and Authorization carries
        Basic, that's recognized as passthrough — gives JWT-not-required
        deployments the Swagger-UI-native experience without the custom
        header friction."""
        encoded = base64.b64encode(b"alice:secret").decode()
        req = _request({"authorization": f"Basic {encoded}"})
        assert extract_basic_creds(req) == ("alice", "secret")

    def test_x_db_authorization_takes_precedence_over_standard(self):
        """When both headers carry Basic credentials, the explicit
        X-DB-Authorization wins — preserves back-compat for deployments
        that use the explicit form."""
        explicit = base64.b64encode(b"explicit:one").decode()
        standard = base64.b64encode(b"standard:two").decode()
        req = _request({
            "authorization": f"Basic {standard}",
            "x-db-authorization": f"Basic {explicit}",
        })
        assert extract_basic_creds(req) == ("explicit", "one")


class TestHashUsername:
    def test_returns_redacted_when_secret_missing(self, monkeypatch):
        monkeypatch.delenv("LOG_USER_SECRET", raising=False)
        assert hash_username("alice") == "<redacted>"

    def test_returns_hash_when_secret_set(self, monkeypatch):
        monkeypatch.setenv("LOG_USER_SECRET", "test-secret")
        result = hash_username("alice")
        assert result.startswith("user:")
        assert len(result) == 5 + 16  # "user:" + 16 hex chars

    def test_deterministic(self, monkeypatch):
        monkeypatch.setenv("LOG_USER_SECRET", "test-secret")
        assert hash_username("alice") == hash_username("alice")

    def test_different_users_different_hashes(self, monkeypatch):
        monkeypatch.setenv("LOG_USER_SECRET", "test-secret")
        assert hash_username("alice") != hash_username("bob")

    def test_different_secrets_different_hashes(self, monkeypatch):
        monkeypatch.setenv("LOG_USER_SECRET", "secret-a")
        a = hash_username("alice")
        monkeypatch.setenv("LOG_USER_SECRET", "secret-b")
        b = hash_username("alice")
        assert a != b

    def test_redact_replaces_username_in_message(self, monkeypatch):
        monkeypatch.setenv("LOG_USER_SECRET", "test-secret")
        msg = 'password authentication failed for user "alice"'
        redacted = redact_username(msg, "alice")
        assert "alice" not in redacted
        assert "user:" in redacted

    def test_redact_noop_when_username_empty(self):
        assert redact_username("some error", "") == "some error"
