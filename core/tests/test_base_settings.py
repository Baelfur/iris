"""Tests for AppSettings shared fields.

Defaults must match the documented values; env-var overrides must take
effect through Pydantic's normal mechanism.
"""

import pytest

from core.config.settings import AppSettings


class _ConcreteSettings(AppSettings):
    """Minimal subclass — no DB-specific required fields."""


@pytest.fixture(autouse=True)
def _set_required_fields(monkeypatch):
    """``CONFIG_SOURCE`` (#107) and ``AUTH__MODE`` (#261) are required.
    Set defaults so tests not exercising those paths can instantiate
    ``_ConcreteSettings`` without env friction. Tests that DO exercise
    them override or unset via monkeypatch."""
    monkeypatch.setenv("CONFIG__SOURCE", "local")
    monkeypatch.setenv("AUTH__MODE", "gateway")


class TestQueryTimeoutSeconds:
    """#17 — per-query timeout is configurable; default 30s."""

    def test_default_is_thirty_seconds(self):
        s = _ConcreteSettings()
        assert s.pool.query_timeout_seconds == 30

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("POOL__QUERY_TIMEOUT_SECONDS", "45")
        s = _ConcreteSettings()
        assert s.pool.query_timeout_seconds == 45

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("POOL__QUERY_TIMEOUT_SECONDS", "0")
        s = _ConcreteSettings()
        assert s.pool.query_timeout_seconds == 0


class TestDeploymentName:
    """#97 — DEPLOYMENT_NAME accepts Kubernetes-style identifiers.
    Hyphens flow through to header/log/health as-is and only get normalized
    to underscores when DbSource materializes the value as a Postgres
    unquoted identifier (#254)."""

    def test_default_is_empty(self):
        assert _ConcreteSettings().deployment_name == ""

    def test_valid_lowercase_identifier(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_NAME", "inventory")
        assert _ConcreteSettings().deployment_name == "inventory"

    def test_valid_with_underscore(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_NAME", "inventory_prod")
        assert _ConcreteSettings().deployment_name == "inventory_prod"

    def test_valid_with_hyphen(self, monkeypatch):
        """Kubernetes / Helm / Docker names use hyphens pervasively; the
        value passes through to header / log / health unchanged."""
        monkeypatch.setenv("DEPLOYMENT_NAME", "inventory-demo")
        assert _ConcreteSettings().deployment_name == "inventory-demo"

    def test_uppercase_rejected(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_NAME", "Inventory")
        with pytest.raises(ValueError, match="DEPLOYMENT_NAME"):
            _ConcreteSettings()

    def test_special_char_rejected(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_NAME", "inv;DROP")
        with pytest.raises(ValueError, match="DEPLOYMENT_NAME"):
            _ConcreteSettings()

    def test_starts_with_digit_rejected(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_NAME", "1inv")
        with pytest.raises(ValueError, match="DEPLOYMENT_NAME"):
            _ConcreteSettings()

    def test_too_long_rejected(self, monkeypatch):
        # 64 chars — one over the Postgres identifier max
        monkeypatch.setenv("DEPLOYMENT_NAME", "a" * 64)
        with pytest.raises(ValueError, match="DEPLOYMENT_NAME"):
            _ConcreteSettings()

    def test_max_length_accepted(self, monkeypatch):
        # 63 chars — exactly at the limit
        monkeypatch.setenv("DEPLOYMENT_NAME", "a" * 63)
        assert _ConcreteSettings().deployment_name == "a" * 63


class TestConfigSource:
    """#98 — CONFIG_SOURCE validation. Required as of #107."""

    def test_unset_rejected_with_guidance(self, monkeypatch):
        """``CONFIG__SOURCE`` is a required field — Pydantic's missing-field
        message names the field path so operators see ``config`` in the
        traceback."""
        monkeypatch.delenv("CONFIG__SOURCE", raising=False)
        with pytest.raises(ValueError, match="config"):
            _ConcreteSettings()

    def test_accepts_local_git_db(self, monkeypatch):
        for v in ("local", "git", "db"):
            monkeypatch.setenv("CONFIG__SOURCE", v)
            assert _ConcreteSettings().config.source == v

    def test_unknown_value_rejected(self, monkeypatch):
        """Pydantic's Literal coercion canonicalizes the rejection message
        as ``Input should be 'local', 'git' or 'db'``."""
        monkeypatch.setenv("CONFIG__SOURCE", "ftp")
        with pytest.raises(ValueError, match="local"):
            _ConcreteSettings()


class TestAuthMode:
    """#261 — AUTH__MODE is required (no default). Forces operators to
    declare jwt / gateway / open rather than inheriting open from omission.
    AUTH__JWKS_URL is required when mode=jwt."""

    def test_unset_rejected(self, monkeypatch):
        monkeypatch.delenv("AUTH__MODE", raising=False)
        with pytest.raises(ValueError, match="auth"):
            _ConcreteSettings()

    def test_accepts_jwt_gateway_open(self, monkeypatch):
        monkeypatch.setenv("AUTH__JWKS_URL", "https://idp.example.com/jwks")
        for mode in ("jwt", "gateway", "open"):
            monkeypatch.setenv("AUTH__MODE", mode)
            assert _ConcreteSettings().auth.mode == mode

    def test_unknown_value_rejected(self, monkeypatch):
        monkeypatch.setenv("AUTH__MODE", "loose")
        with pytest.raises(ValueError, match="jwt"):
            _ConcreteSettings()

    def test_jwt_requires_jwks_url(self, monkeypatch):
        monkeypatch.setenv("AUTH__MODE", "jwt")
        monkeypatch.delenv("AUTH__JWKS_URL", raising=False)
        with pytest.raises(ValueError, match="JWKS_URL"):
            _ConcreteSettings()

    def test_gateway_doesnt_require_jwks_url(self, monkeypatch):
        monkeypatch.setenv("AUTH__MODE", "gateway")
        monkeypatch.delenv("AUTH__JWKS_URL", raising=False)
        s = _ConcreteSettings()
        assert s.auth.mode == "gateway"
        assert s.auth.jwks_url == ""


class TestRequirePassthrough:
    """#261 — AUTH__REQUIRE_PASSTHROUGH defaults true (fail-closed). Data
    routes 401 without passthrough creds unless explicitly disabled."""

    def test_default_true(self):
        assert _ConcreteSettings().auth.require_passthrough is True

    def test_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("AUTH__REQUIRE_PASSTHROUGH", "false")
        assert _ConcreteSettings().auth.require_passthrough is False


class TestMaxPageSizeDefault:
    """#261 — MAX_PAGE_SIZE defaults 10000 (was 0/unbounded). 0 is the
    explicit-opt-out value for operators who genuinely want unbounded."""

    def test_default_is_10000(self):
        assert _ConcreteSettings().max_page_size == 10000

    def test_zero_still_accepted(self, monkeypatch):
        monkeypatch.setenv("MAX_PAGE_SIZE", "0")
        assert _ConcreteSettings().max_page_size == 0


class TestAllowlistMode:
    """#291 — ALLOWLIST__MODE controls whether allowlist.yaml is a
    security boundary (enforce, default) or a spec-only filter
    (presentation)."""

    def test_default_is_enforce(self):
        assert _ConcreteSettings().allowlist_mode == "enforce"

    def test_accepts_enforce_and_presentation(self, monkeypatch):
        for mode in ("enforce", "presentation"):
            monkeypatch.setenv("ALLOWLIST_MODE", mode)
            assert _ConcreteSettings().allowlist_mode == mode

    def test_unknown_value_rejected(self, monkeypatch):
        monkeypatch.setenv("ALLOWLIST_MODE", "loose")
        with pytest.raises(ValueError, match="enforce"):
            _ConcreteSettings()


class TestExtraForbid:
    """#249 — extra='forbid' on the root settings and every submodel
    catches typos in nested env-var names at startup. Limitation:
    pydantic-settings's EnvSettingsSource never emits unknown flat-
    root env vars as extra keys, so typos in flat-root names are NOT
    caught — only nested-submodel typos.

    Documents both the covered case (nested typo → ValidationError)
    and the uncovered case (flat-root typo silently dropped) so a
    future regression that flips one without the other is detected."""

    def test_nested_typo_in_auth_rejected(self, monkeypatch):
        """AUTH__JWKS_LUR (typo of JWKS_URL) raises ValidationError."""
        monkeypatch.setenv("AUTH__JWKS_LUR", "https://idp/.well-known/jwks")
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _ConcreteSettings()

    def test_nested_typo_in_pool_rejected(self, monkeypatch):
        """POOL__MIN_SIE (typo of MIN_SIZE) raises ValidationError."""
        monkeypatch.setenv("POOL__MIN_SIE", "5")
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _ConcreteSettings()

    def test_nested_typo_in_kafka_rejected(self, monkeypatch):
        """KAFKA__TOPCI (typo of TOPIC) raises ValidationError."""
        monkeypatch.setenv("KAFKA__TOPCI", "wrong")
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _ConcreteSettings()

    def test_nested_typo_in_circuit_breaker_rejected(self, monkeypatch):
        """CIRCUIT_BREAKER__ENABLD (typo of ENABLED) raises ValidationError."""
        monkeypatch.setenv("CIRCUIT_BREAKER__ENABLD", "true")
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _ConcreteSettings()

    def test_nested_typo_in_config_rejected(self, monkeypatch):
        """CONFIG__GIT_BRNCH (typo of GIT_BRANCH) raises ValidationError."""
        monkeypatch.setenv("CONFIG__GIT_BRNCH", "main")
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _ConcreteSettings()

    def test_known_nested_field_still_works(self, monkeypatch):
        """Sanity: forbid doesn't break legitimate nested overrides."""
        monkeypatch.setenv("POOL__MIN_SIZE", "5")
        monkeypatch.setenv("AUTH__AUDIENCE", "my-app")
        s = _ConcreteSettings()
        assert s.pool.min_size == 5
        assert s.auth.audience == "my-app"

    def test_flat_root_typo_silently_dropped_documented_limitation(
        self, monkeypatch
    ):
        """Pydantic-settings's EnvSettingsSource doesn't emit unknown
        flat-root env vars as extra dict keys, so extra='forbid' on the
        root model never sees them. This test pins the limitation so a
        future change that closes this gap surfaces here."""
        monkeypatch.setenv("MAX_PAGE_SIE", "1")  # typo of MAX_PAGE_SIZE
        # No exception — typo silently dropped, default 10000 still applies.
        s = _ConcreteSettings()
        assert s.max_page_size == 10000

    def test_direct_init_typo_rejected(self):
        """Direct dict-style instantiation with an unknown field raises.
        Mostly hits in tests and code; production paths go through the
        env-var source."""
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            _ConcreteSettings(some_unknown_field="x")


class TestErrorDetail:
    """#82 — ERROR_DETAIL accepts terse/safe/verbose; default is terse."""

    def test_default_is_terse(self):
        assert _ConcreteSettings().error_detail == "terse"

    def test_accepts_terse_safe_verbose(self, monkeypatch):
        for v in ("terse", "safe", "verbose"):
            monkeypatch.setenv("ERROR_DETAIL", v)
            assert _ConcreteSettings().error_detail == v

    def test_unknown_value_rejected(self, monkeypatch):
        """Pydantic's ``str`` Enum coercion rejects unknown values with the
        canonical "Input should be 'terse', 'safe' or 'verbose'" message."""
        monkeypatch.setenv("ERROR_DETAIL", "loud")
        with pytest.raises(ValueError, match="terse"):
            _ConcreteSettings()


class TestPoolSizing:
    """Defaults documented in the operating-guide env reference. (#62)"""

    def test_pool_min_default(self):
        assert _ConcreteSettings().pool.min_size == 2

    def test_pool_max_default(self):
        assert _ConcreteSettings().pool.max_size == 10

    def test_hpa_max_default(self):
        assert _ConcreteSettings().pool.hpa_max_replicas == 10


class TestLogLevel:
    """``LOG_LEVEL`` env var (#356) — root logger level, default INFO."""

    def test_default_is_info(self):
        assert _ConcreteSettings().log_level == "INFO"

    def test_env_override_uppercase(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        assert _ConcreteSettings().log_level == "DEBUG"

    def test_env_override_lowercase_normalized(self, monkeypatch):
        """``LOG_LEVEL=debug`` should be accepted — the field validator
        uppercases before the Literal check so operators don't have to
        remember the casing convention."""
        monkeypatch.setenv("LOG_LEVEL", "debug")
        assert _ConcreteSettings().log_level == "DEBUG"

    def test_env_override_mixed_case_normalized(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "Warning")
        assert _ConcreteSettings().log_level == "WARNING"

    def test_unknown_value_rejected(self, monkeypatch):
        """Anything outside the standard Python levels is rejected with
        Pydantic's canonical Literal error — defense against typos."""
        monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
        with pytest.raises(ValueError, match="DEBUG"):
            _ConcreteSettings()
