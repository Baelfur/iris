"""Shared settings fields consumed by core. Each variant extends this with DB-specific fields.

Organized as **nested submodels** that compose into ``AppSettings``.
Each submodel owns its fields, defaults, validators, and the inline
documentation for the env-var surface it exposes. Access is structural
— ``settings.auth.jwks_url``, ``settings.kafka.brokers``,
``settings.pool.max_size`` — and env vars use double-underscore
nesting (``AUTH__JWKS_URL``, ``KAFKA__BROKERS``, ``POOL__MAX_SIZE``).

A handful of cross-cutting fields stay flat at the root because they
don't naturally cluster: ``deployment_name`` (identity),
``allowed_schemas`` (DDL surface), ``error_detail`` /
``max_page_size`` / ``readiness_timeout_ms`` (response shape),
``enable_metrics`` (single toggle).

Variants extend ``AppSettings`` with database-specific flat
fields (``pg_host``, ``oracle_host``, etc.); those don't go through
nested submodels because each variant has only one connection-config
group worth of fields.
"""

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ConfigSource = Literal["local", "git", "db"]

OpenAPIRenderMode = Literal["simple-schema", "optimized-schema", "full-schema"]

# Auth posture for the user-facing surface. Required (no default) — operators
# must declare a posture rather than getting one by omission.
#   jwt     — the service validates inbound Authorization: Bearer JWT against the
#             configured JWKS. Requires AUTH__JWKS_URL.
#   gateway — the service accepts any caller; documented assumption is that an
#             upstream gateway / proxy already validated identity. Same
#             runtime behavior as the historical "JWKS unset" posture, but
#             declared rather than implicit.
#   open    — the service accepts any caller, no upstream assumption. Dev / inside-
#             trust-perimeter only. Logs a startup WARNING.
AuthMode = Literal["jwt", "gateway", "open"]

# Allowlist semantics. Default ``enforce`` preserves today's behavior:
# ``allowlist.yaml`` narrows the DDL cache (security boundary; non-listed
# tables 404). ``presentation`` keeps the full DDL surface reachable but
# applies the allowlist matchers at OpenAPI render time only — the spec
# documents the curated subset while the underlying API stays open. Use
# ``presentation`` for big estates where operators want a tidy docs
# surface without losing direct-URL access.
AllowlistMode = Literal["enforce", "presentation"]

# OpenAPI exposure shape for the user-facing surface. Default ``enabled``
# preserves today's behavior (`/docs`, `/redoc`, `/openapi.json` all
# reachable unauthenticated). ``admin-enabled`` disables the HTML pages
# and gates `/openapi.json` behind the same `X-Admin-Token` that gates
# `/admin/*`. ``disabled`` returns 404 for all three. The admin sub-app
# (`/admin/docs`, `/admin/openapi.json`) is unaffected — already token-
# gated by the sub-app dependency.
OpenAPIVisibility = Literal["enabled", "admin-enabled", "disabled"]


# Standard Python logging levels. Set on the root logger so module
# loggers (`getLogger(__name__)`) inherit. The uvicorn access
# logger is independently pinned to WARNING regardless — it duplicates
# the service's own request log line and is silenced deliberately.
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ErrorDetail(StrEnum):
    """Three-way enum for ``ERROR_DETAIL``.

    String-valued so YAML / env vars can supply the literal name
    (``ERROR_DETAIL=safe``) and Pydantic coerces it. Code branches
    using ``settings.error_detail == ErrorDetail.SAFE`` get
    autocomplete and type-checking; old string-equality checks
    (``== "safe"``) still work because the enum members compare equal
    to their values.
    """

    TERSE = "terse"
    SAFE = "safe"
    VERBOSE = "verbose"


# Lowercase letter start, then alnum + underscore + hyphen, max 63 chars.
# Hyphens are accepted because Kubernetes / Helm / Docker names use them
# pervasively. DbSource normalizes hyphens to underscores when materializing
# the value as a Postgres unquoted identifier.
_DEPLOYMENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")

# APP_NAME drives the application's external identity — HTTP header
# prefix, FastAPI / OpenAPI title, OTel service.name fallback, and the
# `app` field on JSON log records. Constrained to letters/digits/hyphens
# (no spaces) so the same value cleanly cascades to all those surfaces.
# Mixed case is allowed so operators can pick `"the service"` (acronym) vs
# `"resource-direct"` (kebab) per their brand.
_APP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,62}$")


def app_header_slug(app_name: str) -> str:
    """Derive the HTTP-header slug from APP_NAME.

    Title-cases each hyphen-separated segment so the value drops cleanly
    into a header name like ``X-{slug}-Deployment``. Examples:

    - ``"app"`` → ``"App"`` (→ ``X-App-Deployment``) — default
    - ``"resource-direct"`` → ``"Resource-Direct"`` (→ ``X-Resource-Direct-Deployment``)
    - ``"the service"`` → ``"the service"`` (operator's caps preserved when supplied)

    Title-case the segment only if it's all-lowercase; preserve operator-
    supplied caps (so ``"the service"`` stays as a recognizable acronym).
    """
    return "-".join(p if not p.islower() else p.title() for p in app_name.split("-"))


class AuthSettings(BaseModel):
    """Three credential lanes plus a posture declaration:

    - ``AUTH__MODE`` (required, no default) declares the user-facing
      posture: ``jwt`` / ``gateway`` / ``open``. Operators opt into one
      explicitly rather than inheriting it from omission.
    - ``AUTH__JWKS_URL`` (+ optional ``AUTH__AUDIENCE`` /
      ``AUTH__ISSUER``) configures JWT validation. Required when
      ``AUTH__MODE=jwt``; ignored otherwise.
    - ``AUTH__REQUIRE_PASSTHROUGH`` (default ``True``) makes the data
      routes (``/{schema}/{table}`` and ``/queries/<path>``) reject
      requests without ``X-DB-Authorization`` (or ``Authorization: Basic``)
      at the service layer — 401 before the DB call. Operators running with
      a metadata-only service account *and* wanting pool-mode access
      for some calls (e.g., internal dashboards) explicitly set
      ``AUTH__REQUIRE_PASSTHROUGH=false``.
    - ``AUTH__ADMIN_TOKEN`` is the shared secret required on
      ``X-Admin-Token`` for ``/admin/*``. Independent of the JWT lane;
      admin actions typically come from CD pipelines / runbooks
      without a meaningful end-user identity. Unset → admin endpoints
      fail closed (401, "admin token not configured"). DB credential
      passthrough lives on a third header (``X-DB-Authorization``)
      and isn't a settings concern — see :mod:`core.auth.creds`.
    """

    model_config = ConfigDict(extra="forbid")

    mode: AuthMode
    jwks_url: str = ""
    audience: str = ""
    issuer: str = ""
    admin_token: str = ""
    require_passthrough: bool = True
    # Hybrid SSO admin. When ``admin_group`` is set, ``/admin/*``
    # also accepts a Bearer JWT whose configured claim contains the
    # group value — alongside the existing ``X-Admin-Token`` path. Both
    # can be configured simultaneously; token wins when both headers
    # are present. Leave ``admin_group`` empty to keep today's token-only
    # behavior bit-for-bit.
    admin_group: str = ""
    # Claim name to look in for the admin group. Common shapes:
    # ``groups`` (list of group names), ``roles`` (same), ``scope``
    # (space-delimited string — handled specially). Operators set this
    # to whatever their IDP emits.
    admin_claim_name: str = "groups"
    # OpenAPI spec OIDC metadata. When all three are set, the spec
    # declares an ``oauth2`` securityScheme so Swagger UI's Authorize
    # button runs the OIDC popup against the IDP. Otherwise spec users
    # fall back to the manual Bearer-paste flow.
    oidc_auth_url: str = ""
    oidc_token_url: str = ""
    oidc_client_id: str = ""

    @model_validator(mode="after")
    def _require_jwks_when_mode_jwt(self) -> "AuthSettings":
        if self.mode == "jwt" and not self.jwks_url:
            raise ValueError("AUTH__JWKS_URL is required when AUTH__MODE=jwt")
        return self

    @model_validator(mode="after")
    def _require_jwks_when_admin_group_set(self) -> "AuthSettings":
        # The JWT-admin path uses the same JWKS as the user-facing JWT
        # lane (one IDP, one signing key set). Enabling admin_group
        # without JWKS configured would fail every JWT-admin request at
        # runtime with a less-clear error; reject up front at startup.
        if self.admin_group and not self.jwks_url:
            raise ValueError(
                "AUTH__ADMIN_GROUP requires AUTH__JWKS_URL "
                "(the JWT-admin path validates against the same JWKS as AUTH__MODE=jwt)"
            )
        return self


class PoolSettings(BaseModel):
    """Connection-pool sizing + per-query timeout.

        ``POOL__MIN_SIZE`` / ``POOL__MAX_SIZE`` bound the pool per pod
    . Trino doesn't pool conventionally and ignores both.
        ``POOL__HPA_MAX_REPLICAS`` feeds the startup pool-sizing report
     — the fleet-capacity math is logged as advice, never
        auto-applied; default matches the shipped HPA manifests.
        ``POOL__QUERY_TIMEOUT_SECONDS`` enforces a DB-side timeout via each
        variant's native mechanism (Postgres ``statement_timeout``, MySQL
        ``MAX_EXECUTION_TIME``, MariaDB ``max_statement_time``, Oracle
        ``call_timeout``, Trino ``query_max_execution_time``); 0 disables
    .
    """

    model_config = ConfigDict(extra="forbid")

    min_size: int = 2
    max_size: int = 10
    query_timeout_seconds: int = 30
    hpa_max_replicas: int = 10


class CircuitBreakerSettings(BaseModel):
    """Optional async circuit breaker around DB calls.

    Default off — only useful on infrastructure with transient
    outages where shedding load is preferable to every request
    blocking on its driver timeout. When ``CIRCUIT_BREAKER__ENABLED``
    is true, ``CIRCUIT_BREAKER__FAIL_MAX`` consecutive failures open
    the breaker and subsequent calls return 503 + ``Retry-After`` for
    ``CIRCUIT_BREAKER__RESET_TIMEOUT`` seconds.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    fail_max: int = 5
    reset_timeout: float = 5.0


class ConfigSettings(BaseModel):
    """Where ``validation/`` + ``queries/`` YAMLs come from.

    ``CONFIG__SOURCE=local`` reads from the path in
    ``CONFIG__LOCAL_ROOT`` (default: cwd). ``git`` clones an external
    repo on startup; set ``CONFIG__GIT_URL`` (and optionally
    ``CONFIG__GIT_BRANCH`` / ``CONFIG__GIT_TOKEN``). ``db`` reads from
    a per-deployment database on a config Postgres; set
    ``CONFIG__DB_DSN`` to the admin DB (typically ``postgres``).
    ``DEPLOYMENT_NAME`` is required in db mode — it's the
    per-deployment database name; the service account needs
    ``CREATEDB`` on first startup.

    ``CONFIG__SOURCE`` is a required ``Literal["local", "git", "db"]``
    field — Pydantic rejects unset / unknown values at lifespan startup
    with a clear message and a list of valid choices. Fail-closed
    posture matches ``AUTH__ADMIN_TOKEN`` (empty → admin endpoints 401),
    explicitly NOT ``ALLOWED_SCHEMAS`` (empty → grants-driven open
    default). Operators must opt into a source rather than fall
    back to whatever the image baked.

    ``CONFIG__LOCAL_ROOT`` only applies in local mode. Defaults to
    ``"."`` (cwd-relative); operators with custom directory layouts
    point it elsewhere. Variant integration tests use this to point
    at ``tests/fixtures/`` so test YAMLs don't have to live at the
    deploy artifact root.
    """

    model_config = ConfigDict(extra="forbid")

    source: ConfigSource
    local_root: str = "."
    git_url: str = ""
    git_branch: str = "main"
    git_token: str = ""
    db_dsn: str = ""


class KafkaSettings(BaseModel):
    """Optional Kafka log-stream sink.

    When ``KAFKA__BROKERS`` is set, the root logger gets a
    KafkaHandler that publishes the same JSON envelope going to
    stdout. Default off — operators not running Kafka don't need the
    dep installed and don't get the network-sink risk surface.
    Multiple the service producers can target one topic; consumers demux on
    the ``host`` and ``deployment`` envelope fields (multi-producer
    topology). ``KAFKA__QUEUE_MAX`` bounds the in-process queue
    before drop-and-count kicks in.
    """

    model_config = ConfigDict(extra="forbid")

    brokers: str = ""
    topic: str = "app.events"
    client_id: str = ""  # Empty → auto-derive: <app_name>-<deployment>-<host>
    acks: str = "1"  # 0 = fire-and-forget, 1 = leader, all = full
    queue_max: int = 10000  # Records queued before drop-and-count

    @field_validator("acks")
    @classmethod
    def _validate_acks(cls, v: str) -> str:
        if v not in ("0", "1", "all"):
            raise ValueError("KAFKA__ACKS must be one of: '0', '1', 'all'; got: " + repr(v))
        return v


class AppSettings(BaseSettings):
    """Shared settings fields consumed by core.

        Variants should inherit from this and add database-specific fields
        (e.g. ``pg_host``, ``oracle_host``).

        Fields that don't naturally cluster stay flat at the root:

        - ``DEPLOYMENT_NAME`` — canonical identity. Cascades through log
          records, the ``X-{App}-Deployment`` response header, and the OTel
          ``service.name`` default. Optional in general; required when
          ``CONFIG__SOURCE=db`` because it's the per-deployment Postgres
          database name. Validated against Postgres identifier rules
    .
        - ``ALLOWED_SCHEMAS`` — narrows the DDL-harvest surface. Empty
          (default) defers to the DB role grants; non-empty acts
          as an additional narrowing layer.
        - ``ERROR_DETAIL`` — response-body shaping for driver errors.
          ``terse`` (default) collapses to generic strings; ``safe`` returns
          stable ``{"error": {"code": "db.…", "message": "…"}}``;
          ``verbose`` returns raw driver text plus operator-debug fields.
          Logs always retain the full text (usernames HMAC-redacted)
          regardless of mode.
        - ``MAX_PAGE_SIZE`` — clamps ``$count`` so a misbehaving client
          can't lock up a pool worker. 0 (default) disables the cap.
        - ``READINESS_TIMEOUT_MS`` — caps the ``/ready`` DB ping. 0 skips
          the DB hit and makes readiness equivalent to ``/health``.
        - ``ENABLE_METRICS`` — mounts ``/metrics`` with Prometheus-format
          counters when true. Requires the ``[metrics]`` extra. Tracing
          doesn't have an equivalent toggle — it activates on the
          presence of ``OTEL_EXPORTER_OTLP_ENDPOINT``; same
          pattern as ``KAFKA__BROKERS`` for log streaming.
        - ``LOG_USER_SECRET`` — HMAC key for username redaction in error
          logs. When set, usernames become ``user:<16-hex>``; when unset,
          they become ``<redacted>``. Recommended in production so
          operators can correlate failed-auth attempts across pods
          without storing the cleartext PII.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        # `forbid` catches typos in nested-submodel env vars (e.g.,
        # AUTH__JWKS_LUR, POOL__MIN_SIE) by raising a ValidationError at
        # startup. Limitation: pydantic-settings's EnvSettingsSource
        # never emits unknown flat-root env vars (e.g., MAX_PAGE_SIE) as
        # extra keys, so typos in flat-root names are still silently
        # dropped. The vast majority of security-relevant fields are
        # nested, so the dangerous-typo class is covered.
        extra="forbid",
    )

    # Flat root fields (cross-cutting; no natural submodel home)
    # Application brand identity. Cascades to the HTTP response header
    # (``X-{Slug}-Deployment``), FastAPI / OpenAPI title, OTel
    # ``service.name`` fallback, and the ``app`` field on JSON log
    # records. Default ``"app"`` is intentionally neutral so the
    # application doesn't impose a brand on operators; set it to
    # whatever the surrounding product calls this layer.
    app_name: str = "app"
    deployment_name: str = ""
    error_detail: ErrorDetail = ErrorDetail.TERSE
    # Default 10000 (was 0/unbounded) — fail-closed against unbounded
    # page-size requests rather than relying on operators reading the doc
    # recommendation. Operators who genuinely need unbounded set
    # MAX_PAGE_SIZE=0 explicitly.
    max_page_size: int = 10000
    readiness_timeout_ms: int = 500
    enable_metrics: bool = False
    # Root-logger level. Default INFO matches previously hardcoded
    # behavior. Set to DEBUG when diagnosing a specific request; set
    # to WARNING / ERROR in production deployments that want a quieter
    # log stream. Case-insensitive at the env-var layer (Pydantic
    # coerces); the field type is the standard Python logging level
    # name. uvicorn.access is pinned to WARNING regardless — see
    # logging_config.setup_logging for the why.
    log_level: LogLevel = "INFO"
    # OpenTelemetry DB-level instrumentation. Opt-in: when true
    # AND ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, each variant registers
    # its driver-specific OTel instrumentor (psycopg / aiomysql /
    # oracledb) so DB calls emit child spans nested under the HTTP
    # request span. Default off because (a) the closed-grammar
    # architecture means SQL is derivable from the URL + path — an
    # operator with the HTTP span and the catalog can reconstruct what
    # query ran without driver instrumentation, and (b) DB spans
    # include the executed SQL by default (sensitive when passthrough
    # users and WHERE values land in the observability backend). Flip
    # on when you need the DB-vs-middleware time breakdown or pool-
    # wait visibility in traces. Trino has no official instrumentor;
    # the toggle is a no-op there with an INFO log at startup.
    enable_db_tracing: bool = False
    log_user_secret: str = ""
    # OpenAPI spec verbosity per harvested table. simple-schema (default)
    # collapses per-column simple-filter params into one generic param
    # with the column list in the description — keeps the spec lean at
    # scale (1000+ table deployments). optimized-schema surfaces only
    # PK/FK/indexed columns as concrete params; non-indexed columns go
    # in the description. full-schema preserves the previously exhaustive
    # behavior — every column gets its own concrete param.
    openapi_render_mode: OpenAPIRenderMode = "simple-schema"
    # OpenAPI exposure shape — see OpenAPIVisibility docstring above. Default
    # `enabled` matches today's behavior; operators close the recon surface
    # with `admin-enabled` (spec behind admin token) or `disabled` (full off).
    #
    openapi_visibility: OpenAPIVisibility = "enabled"
    # Allowlist semantics. Default ``enforce`` (security boundary) preserves
    # today's behavior. ``presentation`` makes the allowlist a spec-only
    # filter — full DDL surface stays reachable, only the OpenAPI spec
    # narrows. See AllowlistMode docstring above.
    allowlist_mode: AllowlistMode = "enforce"
    # HMAC key for cursor pagination tokens. Cursors are
    # base64url(payload).base64url(hmac_sha256(payload)) — the secret
    # binds the payload so callers can't fabricate one that bypasses
    # the original WHERE clause. Leave unset to have the process pick
    # a random 32-byte key at startup; set explicitly (>=32 hex chars
    # / 16+ raw bytes) when cursors need to survive process restarts
    # or be shared across pod replicas. Rotating the secret invalidates
    # all in-flight cursors.
    cursor_secret: str = ""

    # Nested submodels
    auth: AuthSettings  # required; AuthSettings.mode has no default
    pool: PoolSettings = PoolSettings()
    circuit_breaker: CircuitBreakerSettings = CircuitBreakerSettings()
    config: ConfigSettings  # required; ConfigSettings.source has no default
    kafka: KafkaSettings = KafkaSettings()

    @field_validator("deployment_name")
    @classmethod
    def _validate_deployment_name(cls, v: str) -> str:
        if v and not _DEPLOYMENT_NAME_RE.match(v):
            raise ValueError(
                "DEPLOYMENT_NAME must match ^[a-z][a-z0-9_-]{0,62}$ "
                "(Kubernetes-style identifier); got: " + repr(v)
            )
        return v

    @field_validator("app_name")
    @classmethod
    def _validate_app_name(cls, v: str) -> str:
        if not _APP_NAME_RE.match(v):
            raise ValueError(
                "APP_NAME must match ^[A-Za-z][A-Za-z0-9-]{0,62}$ "
                "(letters/digits/hyphens, no spaces — drops cleanly into "
                "HTTP header names and OTel service.name); got: " + repr(v)
            )
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, v):
        """Accept ``LOG_LEVEL=info`` as well as ``LOG_LEVEL=INFO``.

        Python logging conventionally uses UPPERCASE level names; the
        Literal type pins those. Uppercasing here means operators can
        use either case without surprise. Non-string values pass
        through unchanged so the Literal validator surfaces the
        type error normally.
        """
        return v.upper() if isinstance(v, str) else v
