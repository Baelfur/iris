"""Optional OpenTelemetry tracing.

Activates when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set in the environment
(presence-detected). Operators who run an OTel collector set the
endpoint, install the optional ``[tracing]`` extra, and the FastAPI
auto-instrumentation accepts upstream ``traceparent`` headers, emits
one span per HTTP request, and exports via OTLP gRPC. Off when the env
var is unset — no surprise SDK initialization, no surprise deps.

The activation rule across the service: sinks with a required destination
config presence-detect on it (Kafka on ``KAFKA_BROKERS``, tracing on
``OTEL_EXPORTER_OTLP_ENDPOINT``); sinks without one use ``ENABLE_*``
toggles (metrics, because Prometheus is pull-style and has no
destination URL).

Per-variant DB-level instrumentation is opt-in via
``ENABLE_DB_TRACING``. Each variant's ``init_pool`` calls into
this module's ``try_instrument_*`` helpers; when the env flag is unset
or the OTLP endpoint is unset, the helpers no-op.

Coverage (PyPI-published OTel instrumentor availability):

- **psycopg** (postgres) — ``opentelemetry-instrumentation-psycopg``,
  registered when ``[tracing]`` is installed.
- **oracledb** (oracle, async API) — ``opentelemetry-instrumentation-oracledb``,
  best-effort; ``.instrument()`` failures swallowed at WARNING.
- **aiomysql** (mysql + mariadb) — no instrumentor on PyPI for the
  async driver (only sync mysql-connector-python and pymysql are
  covered). Helper logs the gap when enabled.
- **aiotrino** (trino) — no instrumentor available. Helper logs the
  gap when enabled.

Best-effort import for the available cases: when the driver's
instrumentation package isn't installed at runtime, the helper logs
once and continues.

Default off because (a) the closed-grammar architecture makes SQL
derivable from the URL + path — operators with the HTTP span and the
catalog can reconstruct what query ran, so DB-level spans buy
incremental visibility (DB-vs-middleware time breakdown, pool-wait
attribution) rather than baseline visibility, and (b) DB spans
include the executed SQL by default, which is sensitive when
passthrough users and WHERE values land in the observability backend.

Configuration env vars (all read by the OTel SDK directly):

- ``OTEL_EXPORTER_OTLP_ENDPOINT``  — collector URL. Setting this
  activates tracing.
- ``OTEL_SERVICE_NAME``  — span service.name attribute. The service
  defaults to ``<APP_NAME>-<DEPLOYMENT_NAME>`` when unset; the
  operator's explicit value wins.
- ``OTEL_RESOURCE_ATTRIBUTES``  — extra k=v pairs (deployment env,
  cluster, etc.)

Operators who need OTLP HTTP rather than gRPC swap the exporter dep
(``opentelemetry-exporter-otlp-proto-http``) and override the import
in their own setup.
"""

import logging
import os

logger = logging.getLogger(__name__)


def maybe_register(app, settings) -> None:
    """Set up the TracerProvider + OTLP exporter and attach FastAPI auto-
    instrumentation when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.

    Call once per FastAPI app at construction time, after middleware and
    routes are registered, before the lifespan handler runs. The
    instrumentor wraps the app's ASGI middleware stack — every route
    that exists at the time of the call gets request spans.

    Lazy-imports the OpenTelemetry SDK so the deps are only required
    when the endpoint is set; default-off deployments don't need the
    optional ``[tracing]`` extra installed.
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise RuntimeError(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry packages "
            "are not installed. Install with: pip install -e './core[tracing]'"
        ) from exc

    # If the operator hasn't set OTEL_SERVICE_NAME explicitly, default
    # it to "<app_name>-<deployment_name>" so the trace identity matches
    # the rest of the cascade (logs, pod name, X-{AppName}-Deployment
    # header). app_name is operator-configurable. Skipped when
    # OTEL_SERVICE_NAME is already set — operator's explicit choice
    # wins. Also skipped when deployment_name is unset — leaves the SDK
    # to pick its own default ("unknown_service") which is at least an
    # obvious placeholder.
    resource = None
    if settings.deployment_name and "OTEL_SERVICE_NAME" not in os.environ:
        service_name = f"{settings.app_name}-{settings.deployment_name}"
        resource = Resource.create({"service.name": service_name})

    # TracerProvider, exporter, and processor pick up
    # OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_RESOURCE_ATTRIBUTES from env
    # automatically. set_tracer_provider is idempotent in normal use;
    # tests that flip enable_tracing=True should set up isolated providers.
    provider = TracerProvider(resource=resource) if resource else TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    logger.info("OpenTelemetry tracing enabled (OTLP gRPC exporter)")


def _should_instrument_db(settings) -> bool:
    """Shared gate for the try_instrument_* helpers.

    Both conditions must hold: operator-set ``ENABLE_DB_TRACING=true``
    AND the OTLP endpoint is configured. The endpoint check ensures we
    don't pay the instrumentor's runtime cost when nothing's listening.
    """
    if not getattr(settings, "enable_db_tracing", False):
        return False
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def try_instrument_psycopg(settings) -> None:
    """Register OpenTelemetry instrumentation for psycopg 3 if enabled.

    Patches ``psycopg.connect`` and ``psycopg.AsyncConnection.connect``
    so the pool's connection acquisitions get instrumented too. No-op
    when the gate is closed or the optional package is missing.
    """
    if not _should_instrument_db(settings):
        return
    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    except ImportError:
        logger.info(
            "ENABLE_DB_TRACING=true but opentelemetry-instrumentation-psycopg "
            "is not installed; DB spans disabled. Install with: pip install "
            "-e './core[tracing]'"
        )
        return
    PsycopgInstrumentor().instrument()
    logger.info("OpenTelemetry DB tracing enabled (psycopg)")


def try_instrument_aiomysql(settings) -> None:
    """aiomysql (mysql + mariadb variants) has no PyPI-published
    OpenTelemetry instrumentor.

    The OTel ecosystem ships instrumentors for the sync MySQL drivers
    (``opentelemetry-instrumentation-mysql`` for mysql-connector-python,
    ``opentelemetry-instrumentation-pymysql`` for pymysql) but not the
    async ``aiomysql`` driver this service uses. Logs the known gap
    when DB tracing is enabled so operators don't silently miss
    MySQL/MariaDB-side spans. Returns immediately otherwise.
    """
    if not _should_instrument_db(settings):
        return
    logger.info(
        "ENABLE_DB_TRACING=true on MySQL/MariaDB variant — no PyPI-published "
        "OpenTelemetry instrumentor exists for aiomysql; DB-level spans not "
        "emitted. HTTP request spans still work; the URL + path encodes the "
        "query for trace-side derivation."
    )


def try_instrument_oracledb(settings) -> None:
    """Register OpenTelemetry instrumentation for oracledb (async API).

    No-op when the gate is closed or the optional package is missing.
    Async-mode instrumentation in opentelemetry-instrumentation-oracledb
    is less battle-tested than psycopg's; this helper logs the
    activation so operators can verify spans are arriving in the
    backend before relying on them.
    """
    if not _should_instrument_db(settings):
        return
    try:
        from opentelemetry.instrumentation.oracledb import OracleDBInstrumentor
    except ImportError:
        logger.info(
            "ENABLE_DB_TRACING=true but opentelemetry-instrumentation-oracledb "
            "is not installed; DB spans disabled. Install with: pip install "
            "-e './core[tracing]'"
        )
        return
    try:
        OracleDBInstrumentor().instrument()
        logger.info("OpenTelemetry DB tracing enabled (oracledb)")
    except Exception as exc:
        # oracledb's async instrumentation has had rough edges in past
        # releases; never let a tracing-side failure break startup.
        logger.warning("OracleDBInstrumentor.instrument() failed: %s", exc)


def try_instrument_trino(settings) -> None:
    """Trino (aiotrino) has no official OpenTelemetry instrumentor.

    Logs the known gap when DB tracing is enabled so operators don't
    silently miss Trino-side spans without knowing why. Returns
    immediately otherwise.
    """
    if not _should_instrument_db(settings):
        return
    logger.info(
        "ENABLE_DB_TRACING=true on Trino variant — no official aiotrino "
        "OpenTelemetry instrumentor exists; DB-level spans not emitted. "
        "HTTP request spans still work; the Trino URL + path encodes the "
        "query for trace-side derivation."
    )
