"""Cursor pagination token codec tests. (#244)

Pins:
- round-trip: a token made with (orderby, values, secret) verifies and
  returns the values when re-parsed with the same args.
- tamper: flipping a byte in payload or signature fails verification.
- orderby mismatch: a token issued for "id ASC" is rejected when the
  next request's orderby is "id DESC".
- secret rotation: an old token is rejected under a new secret.
- structural failure modes: missing dot, bad base64, bad JSON.
"""

import json

import pytest

from core.engine import cursor as cursor_mod
from core.engine.cursor import CursorError, _b64u_decode, _b64u_encode, make_cursor, parse_cursor

SECRET = b"x" * 32
ORDERBY = "id ASC"


class TestRoundTrip:
    def test_single_column_int_value(self):
        token = make_cursor(ORDERBY, [42], SECRET)
        assert parse_cursor(token, ORDERBY, SECRET) == [42]

    def test_multi_column_mixed_values(self):
        orderby = "category ASC, id ASC"
        token = make_cursor(orderby, ["widgets", 1234], SECRET)
        assert parse_cursor(token, orderby, SECRET) == ["widgets", 1234]

    def test_null_value_round_trips(self):
        token = make_cursor(ORDERBY, [None], SECRET)
        assert parse_cursor(token, ORDERBY, SECRET) == [None]

    def test_unicode_string_value(self):
        token = make_cursor(ORDERBY, ["café"], SECRET)
        assert parse_cursor(token, ORDERBY, SECRET) == ["café"]


class TestTamperDetection:
    def test_flipped_payload_byte_fails(self):
        token = make_cursor(ORDERBY, [42], SECRET)
        payload_b64, sig_b64 = token.split(".")
        # Tamper: re-encode with values=[99] but keep original signature.
        evil_payload = json.dumps(
            {"orderby": ORDERBY, "values": [99]},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        evil_token = f"{_b64u_encode(evil_payload)}.{sig_b64}"
        with pytest.raises(CursorError, match="signature does not verify"):
            parse_cursor(evil_token, ORDERBY, SECRET)

    def test_flipped_signature_byte_fails(self):
        token = make_cursor(ORDERBY, [42], SECRET)
        payload_b64, sig_b64 = token.split(".")
        # Flip the first bit of the signature.
        sig_bytes = bytearray(_b64u_decode(sig_b64))
        sig_bytes[0] ^= 0x01
        evil_token = f"{payload_b64}.{_b64u_encode(bytes(sig_bytes))}"
        with pytest.raises(CursorError, match="signature does not verify"):
            parse_cursor(evil_token, ORDERBY, SECRET)


class TestOrderbyBinding:
    def test_orderby_mismatch_rejected(self):
        token = make_cursor("id ASC", [42], SECRET)
        with pytest.raises(CursorError, match="different .orderby"):
            parse_cursor(token, "id DESC", SECRET)

    def test_orderby_match_required_to_decode(self):
        token = make_cursor("id ASC, name DESC", [42, "x"], SECRET)
        # Same columns, but different direction on the secondary key — reject.
        with pytest.raises(CursorError, match="different .orderby"):
            parse_cursor(token, "id ASC, name ASC", SECRET)


class TestSecretRotation:
    def test_rotated_secret_invalidates_token(self):
        token = make_cursor(ORDERBY, [42], SECRET)
        new_secret = b"y" * 32
        with pytest.raises(CursorError, match="signature does not verify"):
            parse_cursor(token, ORDERBY, new_secret)


class TestStructuralFailures:
    def test_missing_dot_separator(self):
        with pytest.raises(CursorError, match="cursor format invalid"):
            parse_cursor("notacursor", ORDERBY, SECRET)

    def test_empty_token(self):
        with pytest.raises(CursorError, match="cursor format invalid"):
            parse_cursor("", ORDERBY, SECRET)

    def test_bad_base64_payload(self):
        with pytest.raises(CursorError):
            # Valid base64 but the payload won't decode to JSON.
            parse_cursor("!!!.zzzz", ORDERBY, SECRET)

    def test_bad_json_payload(self):
        # Encode a payload that's valid base64url but not JSON.
        bad_payload = b"this is not json"
        # Sign properly so we make it past the HMAC check and into the parse stage.
        import hashlib
        import hmac as _hmac

        sig = _hmac.new(SECRET, bad_payload, hashlib.sha256).digest()
        token = f"{_b64u_encode(bad_payload)}.{_b64u_encode(sig)}"
        with pytest.raises(CursorError, match="not valid JSON"):
            parse_cursor(token, ORDERBY, SECRET)


class TestSecretResolution:
    def test_explicit_string_becomes_bytes(self):
        secret = cursor_mod.get_secret("hello-world")
        assert secret == b"hello-world"

    def test_empty_falls_back_to_random_cached(self):
        # Clear the cached fallback so the test is hermetic.
        cursor_mod._random_fallback = None
        s1 = cursor_mod.get_secret("")
        s2 = cursor_mod.get_secret("")
        assert len(s1) == 32
        # Cached — repeated calls return the same key, so cursors made
        # mid-process round-trip.
        assert s1 is s2

    def test_explicit_value_overrides_cached_fallback(self):
        cursor_mod._random_fallback = None
        _ = cursor_mod.get_secret("")  # populate cache
        explicit = cursor_mod.get_secret("operator-set")
        assert explicit == b"operator-set"
