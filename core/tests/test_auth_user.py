"""Defensive tests for ``core.auth.user.verify_token``. (#260)

Locks in two attack-resistance properties of JWT validation:

1. ``alg: none`` tokens are rejected — PyJWT's ``algorithms=["RS256"]``
   enforces this, but a future refactor that drops the explicit
   ``algorithms`` argument or widens it to include ``"none"`` would
   silently re-open the alg-confusion attack.
2. HS256 tokens are rejected when the JWKS serves an RSA key —
   classic alg-confusion attack: attacker signs an HS256 token using
   the RSA public key as the HMAC secret, hoping the verifier accepts
   it as RS256-with-public-key. PyJWT's strict algorithms list blocks
   this; the test pins the behavior.

Also pins behavior of the mode-aware short-circuit: ``mode="gateway"``
and ``mode="open"`` skip JWT validation entirely (return None, no
header inspection).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from core.auth import user as auth_user


@pytest.fixture
def rsa_keypair():
    """Generate a test RSA keypair. Public key is what the mocked JWKS
    returns; private key is what a legitimate IDP would sign with."""
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
def jwt_mode_context(monkeypatch, rsa_keypair):
    """Wire ``get_context`` (in both auth.user and errors.messages — the
    latter is hit by ``error_msg`` when verify_token raises) to return a
    settings shim with ``auth.mode == "jwt"`` and ``auth.jwks_url`` set,
    plus a fake JWKS client returning the test RSA public key. Also
    resets the module-level ``_jwks_client`` cache so monkeypatching
    takes."""
    from core.errors import messages as error_messages

    _, public_pem = rsa_keypair

    fake_settings = SimpleNamespace(
        auth=SimpleNamespace(
            mode="jwt",
            jwks_url="https://idp.example.com/jwks",
            audience="",
            issuer="",
        ),
        error_detail="terse",
        deployment_name="",
        database="",
    )
    fake_ctx = SimpleNamespace(settings=fake_settings, database="postgresql")
    monkeypatch.setattr(auth_user, "get_context", lambda: fake_ctx)
    monkeypatch.setattr(error_messages, "get_context", lambda: fake_ctx)

    fake_signing_key = SimpleNamespace(key=public_pem)
    fake_jwks = MagicMock()
    fake_jwks.get_signing_key_from_jwt = MagicMock(return_value=fake_signing_key)
    auth_user._jwks_client = fake_jwks
    yield
    auth_user._jwks_client = None


def _request_with_bearer(token: str):
    """Construct a minimal mock Request that ``HTTPBearer`` will accept.

    HTTPBearer reads ``request.headers.get("Authorization")`` with
    title-case capitalization — Starlette's ``Headers`` is case-
    insensitive, but a plain dict isn't. Uppercase key is required."""
    req = MagicMock()
    req.headers = {"Authorization": f"Bearer {token}"}
    return req


class TestModeShortCircuit:
    """When ``auth.mode != "jwt"``, ``verify_token`` returns None without
    reading the Authorization header. Pre-#261 the gate was on
    ``jwks_url`` truthiness; #261 moved it to ``mode``."""

    def test_gateway_mode_returns_none(self, monkeypatch):
        fake_settings = SimpleNamespace(
            auth=SimpleNamespace(mode="gateway", jwks_url="", audience="", issuer="")
        )
        monkeypatch.setattr(
            auth_user, "get_context", lambda: SimpleNamespace(settings=fake_settings)
        )
        # Even with a bogus header, no validation happens.
        req = _request_with_bearer("anything-here")
        # verify_token is async; run it synchronously via asyncio
        import asyncio
        result = asyncio.run(auth_user.verify_token(req))
        assert result is None

    def test_open_mode_returns_none(self, monkeypatch):
        fake_settings = SimpleNamespace(
            auth=SimpleNamespace(mode="open", jwks_url="", audience="", issuer="")
        )
        monkeypatch.setattr(
            auth_user, "get_context", lambda: SimpleNamespace(settings=fake_settings)
        )
        import asyncio
        result = asyncio.run(auth_user.verify_token(_request_with_bearer("anything")))
        assert result is None


class TestAlgConfusionAttacks:
    """``algorithms=["RS256"]`` in jwt.decode rejects alg-confusion
    attacks. These tests pin that behavior so a future refactor can't
    silently widen the accepted-algorithms list."""

    def test_alg_none_rejected(self, jwt_mode_context):
        # Attacker crafts an unsigned token. PyJWT lets you encode
        # alg=none; the verify side must refuse.
        attack_token = jwt.encode({"sub": "alice"}, key="", algorithm="none")
        import asyncio
        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth_user.verify_token(_request_with_bearer(attack_token)))
        assert exc.value.status_code in (401, 403)

    def test_hs256_token_rejected_under_rs256_only(self, jwt_mode_context):
        """Attacker crafts a valid HS256 token (any HMAC secret works
        client-side). The verifier's ``algorithms=["RS256"]`` argument
        means PyJWT refuses the alg before even attempting signature
        check. Closes the alg-confusion attack class regardless of
        what HMAC secret the attacker chose."""
        # 32+ byte secret avoids PyJWT's InsecureKeyLengthWarning. The
        # rejection happens at alg check, not signature verification, so
        # the secret value is irrelevant — but a quiet test is a tidier
        # one.
        attack_token = jwt.encode(
            {"sub": "alice"}, key="x" * 32, algorithm="HS256"
        )
        import asyncio
        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth_user.verify_token(_request_with_bearer(attack_token)))
        assert exc.value.status_code in (401, 403)

    def test_legitimate_rs256_token_accepted(
        self, jwt_mode_context, rsa_keypair
    ):
        """Sanity: a token signed by the matching private key passes."""
        private_pem, _ = rsa_keypair
        good_token = jwt.encode(
            {"sub": "alice"}, key=private_pem, algorithm="RS256"
        )
        import asyncio
        payload = asyncio.run(auth_user.verify_token(_request_with_bearer(good_token)))
        assert payload == {"sub": "alice"}


class TestMissingCredentials:
    """Under ``mode="jwt"``, requests without a Bearer token are 401'd
    even when no token data needs decoding. Note the ``HTTPBearer``
    fixture rejects Basic-on-Authorization (returns None), so a Basic
    header on a JWT-required deployment also hits the missing-creds
    branch — exercised here to lock the behavior."""

    def test_no_authorization_header_401(self, jwt_mode_context):
        req = MagicMock()
        req.headers = {}
        import asyncio
        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth_user.verify_token(req))
        assert exc.value.status_code == 401

    def test_basic_on_authorization_treated_as_missing_under_jwt_mode(
        self, jwt_mode_context
    ):
        """Basic isn't Bearer; HTTPBearer returns None; verify_token
        treats that as missing credentials. Confirms that a Basic
        header doesn't slip past the JWT gate when ``mode=jwt``."""
        import base64
        encoded = base64.b64encode(b"alice:secret").decode()
        req = MagicMock()
        req.headers = {"Authorization": f"Basic {encoded}"}
        import asyncio
        with pytest.raises(HTTPException) as exc:
            asyncio.run(auth_user.verify_token(req))
        assert exc.value.status_code == 401
