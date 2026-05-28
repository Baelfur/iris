"""Tests for core.tracing.maybe_register + DB instrumentation gates.

Tracing activates on presence of ``OTEL_EXPORTER_OTLP_ENDPOINT`` (#134).
DB-level instrumentation is a second gate — requires both the endpoint
*and* ``ENABLE_DB_TRACING=true`` to register (#250). The OpenTelemetry
SDK + auto-instrumentation are optional deps — these tests use mocks so
they don't require ``[tracing]`` to be installed.

Default-off behavior is also asserted at the integration layer
(``test_tracing_does_not_alter_default_app`` in the integration suite).
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.observability.tracing import (
    _should_instrument_db,
    maybe_register,
    try_instrument_aiomysql,
    try_instrument_oracledb,
    try_instrument_psycopg,
    try_instrument_trino,
)


def _settings(
    deployment_name: str = "",
    app_name: str = "app",
    enable_db_tracing: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        deployment_name=deployment_name,
        app_name=app_name,
        enable_db_tracing=enable_db_tracing,
    )


_OTEL_MODULES = (
    "opentelemetry",
    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
    "opentelemetry.instrumentation.fastapi",
    "opentelemetry.sdk.resources",
    "opentelemetry.sdk.trace",
    "opentelemetry.sdk.trace.export",
)


class TestDisabledByDefault:
    def test_no_setup_when_endpoint_unset(self, monkeypatch):
        """OTEL_EXPORTER_OTLP_ENDPOINT unset → no import, no provider
        replaced, no instrumentor attached."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        app = MagicMock()
        # Pre-populate fakes so any accidental import would succeed and
        # leave a trail; we assert nothing was touched.
        fakes = {name: MagicMock() for name in _OTEL_MODULES}
        with patch.dict(sys.modules, fakes):
            maybe_register(app, _settings())
        # No methods invoked on app, none of the OTel mocks called.
        assert not app.method_calls
        for fake in fakes.values():
            assert not fake.method_calls

    def test_disabled_works_without_optional_deps_installed(self, monkeypatch):
        """The lazy import means a default-off deployment doesn't need the
        optional ``[tracing]`` extra present at all."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        app = MagicMock()
        fakes = {name: None for name in _OTEL_MODULES}
        with patch.dict(sys.modules, fakes):
            maybe_register(app, _settings())
        assert not app.method_calls


class TestEnabledPath:
    def _modules(self):
        """Build a fresh module-mock dict per test so prior assertions
        don't bleed across the patch.dict scope."""
        fake_trace = MagicMock()
        modules = {
            "opentelemetry": fake_trace,
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(),
            "opentelemetry.instrumentation.fastapi": MagicMock(),
            "opentelemetry.sdk.resources": MagicMock(),
            "opentelemetry.sdk.trace": MagicMock(),
            "opentelemetry.sdk.trace.export": MagicMock(),
        }
        # set_tracer_provider lives on opentelemetry.trace, which is
        # accessed via `from opentelemetry import trace`.
        fake_trace.trace = MagicMock()
        return modules

    def test_attaches_provider_and_fastapi_instrumentor(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        app = MagicMock()
        modules = self._modules()
        with patch.dict(sys.modules, modules):
            maybe_register(app, _settings())

        sdk_trace = modules["opentelemetry.sdk.trace"]
        sdk_export = modules["opentelemetry.sdk.trace.export"]
        otlp = modules["opentelemetry.exporter.otlp.proto.grpc.trace_exporter"]
        instr = modules["opentelemetry.instrumentation.fastapi"]
        ot = modules["opentelemetry"]

        # No deployment name + no OTEL_SERVICE_NAME → TracerProvider()
        # called with no resource arg (SDK picks its own default).
        sdk_trace.TracerProvider.assert_called_once_with()
        provider = sdk_trace.TracerProvider.return_value
        otlp.OTLPSpanExporter.assert_called_once_with()
        sdk_export.BatchSpanProcessor.assert_called_once_with(
            otlp.OTLPSpanExporter.return_value,
        )
        provider.add_span_processor.assert_called_once_with(
            sdk_export.BatchSpanProcessor.return_value,
        )
        ot.trace.set_tracer_provider.assert_called_once_with(provider)
        instr.FastAPIInstrumentor.instrument_app.assert_called_once_with(app)

    def test_deployment_name_sets_otel_service_name_when_unset(self, monkeypatch):
        """deployment_name + no OTEL_SERVICE_NAME → Resource carries
        service.name=<app_name>-<deployment_name>. (#97, #262)"""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
        app = MagicMock()
        modules = self._modules()
        with patch.dict(sys.modules, modules):
            # Default app_name="app" → service.name="app-inventory".
            maybe_register(app, _settings(deployment_name="inventory"))

        resources = modules["opentelemetry.sdk.resources"]
        resources.Resource.create.assert_called_once_with(
            {"service.name": "app-inventory"},
        )
        # TracerProvider called *with* the resource kwarg.
        sdk_trace = modules["opentelemetry.sdk.trace"]
        sdk_trace.TracerProvider.assert_called_once_with(
            resource=resources.Resource.create.return_value,
        )

    def test_explicit_otel_service_name_wins_over_deployment_name(self, monkeypatch):
        """Operator's OTEL_SERVICE_NAME is the explicit choice — the service
        defers to it even when deployment_name is set. (#97)"""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setenv("OTEL_SERVICE_NAME", "operator-supplied")
        app = MagicMock()
        modules = self._modules()
        with patch.dict(sys.modules, modules):
            maybe_register(app, _settings(deployment_name="inventory"))

        # No Resource constructed — the SDK reads OTEL_SERVICE_NAME from env.
        modules["opentelemetry.sdk.resources"].Resource.create.assert_not_called()
        modules["opentelemetry.sdk.trace"].TracerProvider.assert_called_once_with()

    def test_missing_dep_raises_actionable_error(self, monkeypatch):
        """OTEL_EXPORTER_OTLP_ENDPOINT set without the deps installed
        should fail loudly with an install hint, not a confusing ImportError."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        app = MagicMock()
        with patch.dict(sys.modules, {name: None for name in _OTEL_MODULES}):
            with pytest.raises(RuntimeError, match=r"core\[tracing\]"):
                maybe_register(app, _settings())


class TestEndToEndAgainstRealOTel:
    """Format-validation coverage: build a real FastAPI app, enable
    tracing, swap the exporter for an in-memory one to capture spans,
    hit a route, and assert a span was actually emitted with the
    expected route attribute. Catches wiring regressions the mock-based
    tests can't (e.g. instrumentor not actually wrapping the app, span
    attributes missing).

    Skipped when the optional ``[tracing]`` extra isn't installed —
    matches the runtime contract. CI installs the extra in the
    core job.
    """

    def test_request_emits_span_with_route_attribute(self, monkeypatch):
        pytest.importorskip("opentelemetry")
        pytest.importorskip("opentelemetry.instrumentation.fastapi")

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from opentelemetry import trace
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        # FastAPIInstrumentor's "already instrumented" guard tracks state
        # globally; clear it so a previous test doesn't poison this one.
        FastAPIInstrumentor().uninstrument()

        # Build a fresh app + isolated provider with an in-memory exporter
        # so we can introspect spans without standing up a collector.
        app = FastAPI()

        @app.get("/probe")
        def probe():
            return {"ok": True}

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)

        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.status_code == 200

        spans = exporter.get_finished_spans()
        assert len(spans) >= 1, "FastAPIInstrumentor did not emit a span"
        # The route span name format varies across instrumentor versions
        # ("GET /probe", "/probe", etc.) — assert on the route attribute
        # which is stable.
        route_attrs = [
            s.attributes.get("http.route") or s.attributes.get("http.target")
            for s in spans
        ]
        assert "/probe" in route_attrs, f"no span carried /probe: {route_attrs}"

        # Cleanup so the next test doesn't see this provider.
        FastAPIInstrumentor().uninstrument()


class TestDbInstrumentationGate:
    """``_should_instrument_db`` requires BOTH ``enable_db_tracing=true``
    AND ``OTEL_EXPORTER_OTLP_ENDPOINT`` set. Either alone is no-op. (#250)"""

    def test_off_when_flag_false(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        assert _should_instrument_db(_settings(enable_db_tracing=False)) is False

    def test_off_when_endpoint_unset(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        assert _should_instrument_db(_settings(enable_db_tracing=True)) is False

    def test_on_when_both_set(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        assert _should_instrument_db(_settings(enable_db_tracing=True)) is True

    def test_legacy_settings_without_field_treated_as_disabled(self, monkeypatch):
        """``getattr(..., default=False)`` — a Settings shim missing
        ``enable_db_tracing`` (e.g. a unit test using an old stub) treats
        DB tracing as off rather than raising AttributeError."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        legacy = SimpleNamespace(deployment_name="", app_name="app")
        assert _should_instrument_db(legacy) is False


class TestTryInstrumentHelpers:
    """Per-driver helpers no-op when the gate is closed; on the gated-on
    path they import the matching package and call ``.instrument()``.
    Missing package is logged and swallowed, never propagated. (#250)"""

    @pytest.fixture(autouse=True)
    def gate_open(self, monkeypatch):
        """Open the gate so the helpers reach the import/instrument path."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")

    def test_psycopg_gate_closed_skips_import(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        # Even with the flag on, no endpoint = gate closed = no import.
        fake = MagicMock()
        with patch.dict(sys.modules, {"opentelemetry.instrumentation.psycopg": fake}):
            try_instrument_psycopg(_settings(enable_db_tracing=True))
        assert not fake.method_calls

    def test_psycopg_instruments_when_package_present(self):
        fake_module = MagicMock()
        with patch.dict(
            sys.modules, {"opentelemetry.instrumentation.psycopg": fake_module}
        ):
            try_instrument_psycopg(_settings(enable_db_tracing=True))
        fake_module.PsycopgInstrumentor.assert_called_once_with()
        fake_module.PsycopgInstrumentor.return_value.instrument.assert_called_once_with()

    def test_psycopg_missing_package_logs_and_continues(self, caplog):
        """ImportError on the instrumentor package → INFO log, no raise."""
        with patch.dict(
            sys.modules, {"opentelemetry.instrumentation.psycopg": None}
        ), caplog.at_level("INFO"):
            try_instrument_psycopg(_settings(enable_db_tracing=True))
        assert any("psycopg" in r.message for r in caplog.records)

    def test_aiomysql_logs_documented_gap(self, caplog):
        """aiomysql has no PyPI-published OTel instrumentor — helper
        logs the gap when enabled so MySQL/MariaDB operators aren't
        silently missing spans."""
        with caplog.at_level("INFO"):
            try_instrument_aiomysql(_settings(enable_db_tracing=True))
        assert any("aiomysql" in r.message.lower() for r in caplog.records)

    def test_aiomysql_silent_when_gate_closed(self, caplog):
        """Gate closed → no log line; would otherwise pollute logs for
        every MySQL/MariaDB deployment that doesn't use tracing."""
        with caplog.at_level("INFO"):
            try_instrument_aiomysql(_settings(enable_db_tracing=False))
        assert not any("aiomysql" in r.message.lower() for r in caplog.records)

    def test_oracledb_instrument_failure_is_swallowed(self, caplog):
        """Oracle's async instrumentation has had rough edges — a runtime
        exception during ``.instrument()`` must not break startup."""
        fake_module = MagicMock()
        fake_module.OracleDBInstrumentor.return_value.instrument.side_effect = (
            RuntimeError("oracledb async incompatibility")
        )
        with patch.dict(
            sys.modules, {"opentelemetry.instrumentation.oracledb": fake_module}
        ), caplog.at_level("WARNING"):
            # Must not raise — the helper logs and continues.
            try_instrument_oracledb(_settings(enable_db_tracing=True))
        assert any("OracleDBInstrumentor" in r.message for r in caplog.records)

    def test_trino_logs_documented_gap(self, caplog):
        """Trino has no official instrumentor — when enabled, helper
        logs the gap so operators don't silently miss Trino spans."""
        with caplog.at_level("INFO"):
            try_instrument_trino(_settings(enable_db_tracing=True))
        assert any("aiotrino" in r.message.lower() for r in caplog.records)

    def test_trino_silent_when_gate_closed(self, caplog):
        """Gate closed → no log line; would otherwise pollute logs for
        every Trino deployment that doesn't use tracing."""
        with caplog.at_level("INFO"):
            try_instrument_trino(_settings(enable_db_tracing=False))
        assert not any("aiotrino" in r.message.lower() for r in caplog.records)
