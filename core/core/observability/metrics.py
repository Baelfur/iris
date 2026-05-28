"""Optional Prometheus /metrics endpoint.

Off by default. Operators who run a Prometheus stack flip
``ENABLE_METRICS=true`` and install the optional ``[metrics]`` extra; the
endpoint exposes request-rate, latency histogram, in-flight, and
error-rate signals via prometheus-fastapi-instrumentator. Default-off
matches the broader posture: no surprise routes, no surprise deps.

When unset, no instrumentator is attached, no ``/metrics`` route exists,
the dep doesn't have to be installed, and the import in this module is
deferred.

The endpoint is intentionally **unauthenticated** when enabled — the
expectation is that Prometheus scrapes the service over the pod network
and a network policy / mesh restricts who can reach the route. Operators
who need authenticated metrics should put a sidecar in front (basic-auth
proxy) or scrape from inside the trust perimeter.

The kafka-counter gauges are namespaced by ``APP_NAME`` — the metric
series is ``{app_name}_kafka_records_*``. Default ``APP_NAME=app``
yields ``app_kafka_records_*``; forks set ``APP_NAME`` to their brand
to scope metrics to that brand.
"""

import logging

logger = logging.getLogger(__name__)


def maybe_register(app, settings) -> None:
    """Attach the Prometheus instrumentator if ``ENABLE_METRICS`` is set.

    Call once per FastAPI app at construction time, after middleware and
    before the lifespan handler runs. The instrumentator wraps every
    route added before ``.expose()`` — variants must call this **after**
    ``include_router`` for the metrics labels to cover those routes.

    Lazy-imports prometheus_fastapi_instrumentator so the dep is only
    required when ``ENABLE_METRICS=true``; operators who leave it off
    don't need the optional ``[metrics]`` extra installed.
    """
    if not settings.enable_metrics:
        return
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError as exc:
        raise RuntimeError(
            "ENABLE_METRICS=true but prometheus-fastapi-instrumentator is "
            "not installed. Install with: pip install -e './core[metrics]'"
        ) from exc

    Instrumentator().instrument(app).expose(app)
    if settings.kafka.brokers:
        _register_kafka_gauges(settings.app_name)
    logger.info("Prometheus /metrics endpoint enabled")


def _register_kafka_gauges(app_name: str) -> None:
    """Expose Kafka publish / drop counters as Prometheus gauges.

    Metric names are namespaced by ``APP_NAME`` so the series becomes
    ``{app_name}_kafka_records_*``. Default ``APP_NAME=app`` yields
    ``app_kafka_records_*``; forks set ``APP_NAME`` to scope to their
    brand.

    The kafka_logging module keeps thread-safe ints; we surface them via
    Prometheus gauge callbacks so each scrape reads the live value. No
    coupling to the prometheus client unless metrics is actually enabled.
    """
    try:
        from prometheus_client import Gauge  # ships with the metrics extra
    except ImportError:
        return  # metrics extra missing — nothing to register

    from . import kafka_logging

    # Sanitize the app_name into a Prometheus-friendly metric prefix:
    # Prometheus names allow [a-zA-Z_:][a-zA-Z0-9_:]*. Hyphens (legal in
    # APP_NAME for header-slug purposes) become underscores here.
    prefix = app_name.replace("-", "_")

    # set_function fires per-scrape. Each Gauge's lambda reads one key
    # from a fresh snapshot — three snapshots per scrape, but each takes
    # the lock once. Prometheus scrape rate is ~15s so contention is
    # negligible vs the listener-thread bumps.
    Gauge(
        f"{prefix}_kafka_records_published_total",
        "Total records published to Kafka",
    ).set_function(lambda: kafka_logging.get_counters()["published"])

    Gauge(
        f"{prefix}_kafka_records_dropped_queue_full",
        "Records dropped because the in-process queue was full",
    ).set_function(lambda: kafka_logging.get_counters()["dropped_queue_full"])

    Gauge(
        f"{prefix}_kafka_records_dropped_buffer_full",
        "Records dropped because the producer's local queue was full",
    ).set_function(lambda: kafka_logging.get_counters()["dropped_buffer_full"])

    Gauge(
        f"{prefix}_kafka_records_dropped_producer_error",
        "Records dropped because the producer raised",
    ).set_function(lambda: kafka_logging.get_counters()["dropped_producer_error"])
