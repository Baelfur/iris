"""Tests for core.metrics.maybe_register.

The instrumentator is an optional dep — these tests use mocks so they
don't require ``prometheus-fastapi-instrumentator`` to be installed.

Default-off behavior is also asserted at the integration layer
(``test_metrics_endpoint_off_by_default`` in the integration suite).
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.observability.metrics import maybe_register


def _settings(enable: bool, app_name: str = "app", brokers: str = "") -> SimpleNamespace:
    # ``maybe_register`` reads three fields: the flat ``enable_metrics``
    # toggle, ``app_name`` (drives the kafka-gauge metric prefix per
    # #321), and ``kafka.brokers`` (gates kafka-gauge registration).
    return SimpleNamespace(
        enable_metrics=enable,
        app_name=app_name,
        kafka=SimpleNamespace(brokers=brokers),
    )


class TestDisabledByDefault:
    def test_no_instrumentation_when_disabled(self):
        """enable_metrics=False → no import, no app mutation."""
        app = MagicMock()
        # Even if the module is somehow installed, we shouldn't touch it.
        with patch.dict(sys.modules, {"prometheus_fastapi_instrumentator": MagicMock()}):
            maybe_register(app, _settings(enable=False))
        # No methods invoked on app.
        assert not app.method_calls

    def test_disabled_works_without_optional_dep_installed(self):
        """The lazy import means a default-off deployment doesn't need the
        optional dep present at all."""
        app = MagicMock()
        # Force the module to look uninstalled.
        with patch.dict(sys.modules, {"prometheus_fastapi_instrumentator": None}):
            maybe_register(app, _settings(enable=False))
        assert not app.method_calls


class TestEnabledPath:
    def test_attaches_and_exposes_when_enabled(self):
        app = MagicMock()
        fake_module = MagicMock()
        fake_instrumentator_class = MagicMock()
        fake_module.Instrumentator = fake_instrumentator_class

        with patch.dict(sys.modules, {"prometheus_fastapi_instrumentator": fake_module}):
            maybe_register(app, _settings(enable=True))

        # Pattern: Instrumentator().instrument(app).expose(app)
        fake_instrumentator_class.assert_called_once_with()
        instance = fake_instrumentator_class.return_value
        instance.instrument.assert_called_once_with(app)
        instance.instrument.return_value.expose.assert_called_once_with(app)

    def test_missing_dep_raises_actionable_error(self):
        """enable_metrics=True without the dep installed should fail loudly
        with an install hint, not a confusing ImportError."""
        app = MagicMock()
        # Force the import to fail.
        with patch.dict(sys.modules, {"prometheus_fastapi_instrumentator": None}):
            with pytest.raises(RuntimeError, match=r"core\[metrics\]"):
                maybe_register(app, _settings(enable=True))


class TestEndToEndAgainstRealInstrumentator:
    """Format-validation coverage: spin up a minimal FastAPI app, register
    real Prometheus metrics on it, hit /metrics, and assert the response
    is well-formed Prometheus exposition. Catches wiring regressions the
    mock-based tests can't (e.g. expose() not called, content-type wrong).

    Skipped when the optional ``[metrics]`` extra isn't installed — that
    matches the runtime contract (default-off deployments don't need the
    dep at all). CI installs the extra in the core job so this runs
    there.
    """

    def test_metrics_endpoint_returns_prometheus_exposition(self):
        pytest.importorskip("prometheus_fastapi_instrumentator")

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/probe")
        def probe():
            return {"ok": True}

        maybe_register(app, _settings(enable=True))

        client = TestClient(app)
        # Hit the probe so there's at least one observed request to
        # report. Without traffic the histograms exist but have no
        # series; we want to assert the live shape.
        client.get("/probe")
        resp = client.get("/metrics")

        assert resp.status_code == 200
        # Prometheus exposition format is text/plain with a version
        # parameter — the instrumentator sets it via the
        # prometheus_client default.
        assert "text/plain" in resp.headers["content-type"]
        body = resp.text
        # The instrumentator's default metrics include request totals
        # and latency histograms — both must appear once a request has
        # been observed.
        assert "http_requests_total" in body or "http_request" in body
        # /probe must be labeled and counted.
        assert "/probe" in body


def _unregister_kafka_records(registry, prefix: str) -> None:
    """Tear down the gauges a maybe_register call attached to the default
    Prometheus REGISTRY, so the next test starts clean. Prometheus
    collectors track names as lists; iterate defensively."""
    for coll in list(registry._collector_to_names):
        names = registry._collector_to_names.get(coll, [])
        if isinstance(names, (str, bytes)):
            names = [names]
        if any(str(n).startswith(f"{prefix}_kafka_records_") for n in names):
            try:
                registry.unregister(coll)
            except (KeyError, ValueError):
                pass


class TestKafkaGaugeNamespace:
    """Prometheus metric names are namespaced by APP_NAME so forks rebrand
    the entire metric series with one env-var change. (#321)"""

    def test_default_app_name_yields_app_prefix(self):
        """`APP_NAME=app` (default) → `app_kafka_records_*`."""
        pytest.importorskip("prometheus_client")
        from prometheus_client import REGISTRY

        # Use a real prometheus client to assert the registered names.
        # maybe_register skips kafka gauges when brokers="", so set them.
        app = MagicMock()
        with patch.dict(sys.modules, {
            "prometheus_fastapi_instrumentator": MagicMock(),
        }):
            maybe_register(app, _settings(enable=True, app_name="app", brokers="localhost:9092"))

        names = {m.name for m in REGISTRY.collect()}
        assert "app_kafka_records_published_total" in names
        assert "app_kafka_records_dropped_queue_full" in names
        assert "app_kafka_records_dropped_buffer_full" in names
        assert "app_kafka_records_dropped_producer_error" in names

        # Clean up: unregister so other tests don't see duplicates.
        _unregister_kafka_records(REGISTRY, "app")

    def test_iris_brand_restores_historical_names(self):
        """Operators on the historical brand set `APP_NAME=iris` and the
        old `iris_kafka_records_*` series re-emerges."""
        pytest.importorskip("prometheus_client")
        from prometheus_client import REGISTRY

        app = MagicMock()
        with patch.dict(sys.modules, {
            "prometheus_fastapi_instrumentator": MagicMock(),
        }):
            maybe_register(app, _settings(enable=True, app_name="iris", brokers="localhost:9092"))

        names = {m.name for m in REGISTRY.collect()}
        assert "iris_kafka_records_published_total" in names

        _unregister_kafka_records(REGISTRY, "iris")

    def test_hyphenated_brand_sanitized_to_underscores(self):
        """`APP_NAME=resource-direct` — hyphens aren't legal in Prometheus
        metric names, so the prefix becomes `resource_direct_kafka_*`."""
        pytest.importorskip("prometheus_client")
        from prometheus_client import REGISTRY

        app = MagicMock()
        with patch.dict(sys.modules, {
            "prometheus_fastapi_instrumentator": MagicMock(),
        }):
            maybe_register(
                app,
                _settings(enable=True, app_name="resource-direct", brokers="localhost:9092"),
            )

        names = {m.name for m in REGISTRY.collect()}
        assert "resource_direct_kafka_records_published_total" in names

        _unregister_kafka_records(REGISTRY, "resource_direct")
