"""Optional Kafka log-stream sink.

Off by default. When ``KAFKA_BROKERS`` is set, ``setup_logging`` attaches
a queue-buffered ``KafkaHandler`` to the root logger so the same JSON
envelope going to stdout also lands on a Kafka topic. Default-off matches
``[metrics]`` and ``[tracing]``: no surprise dep, no extra thread, no
extra failure mode for operators not running Kafka.

## Two non-negotiables

1. **Async / queue-buffered.** ``KafkaHandler`` is wrapped via
   ``logging.handlers.QueueHandler``; a ``QueueListener`` runs in a
   background thread and does the real ``producer.produce`` call.
   The synchronous ``logger.info(...)`` call site only enqueues — it
   never blocks on broker reachability. A naive synchronous handler
   would turn every log line into a request-latency dependency.

2. **Drop-on-Kafka-failure, never block-or-crash.** When the broker is
   down or the queue is full, ``KafkaHandler.emit`` catches the
   exception, increments a counter, and returns. We never raise from
   ``emit`` and we never block the service over a logging-path failure. Disk-
   buffer + replay is explicitly out of scope.

## Multi-producer topology

N instances streaming to one topic is a first-class supported
pattern. The defaults reflect that:

- ``client.id`` defaults to ``<APP_NAME>-<deployment-name>-<host>`` (or
  just ``<APP_NAME>-<host>`` when DEPLOYMENT_NAME is unset) so broker-
  side observability doesn't conflate replicas of the same deployment.
- Records are published with no key → round-robin / sticky partitioning
  → no producer becomes a hot partition.
- The ``host`` field in the JSON envelope (set by JSONFormatter) lets
  consumers demux records from individual replicas of one deployment.
"""

import logging
import logging.handlers
import queue
import socket
import threading

logger = logging.getLogger(__name__)

# Module-level counters surfaced by metrics.maybe_register when
# ENABLE_METRICS is set. Kept as plain ints here (not Prometheus
# objects) so the dep stays optional — metrics module reads these.
_published_count = 0
_dropped_queue_full = 0
_dropped_buffer_full = 0
_dropped_producer_error = 0
_counter_lock = threading.Lock()
_listener: logging.handlers.QueueListener | None = None
# Producer reference kept module-global so stop() can flush it on
# shutdown and integration tests can wait for delivery before consuming.
_producer = None
# Queue handler reference kept module-global so a re-init of
# setup_logging (which replaces logging.root.handlers wholesale) can
# re-attach the existing handler without rebuilding the whole stack.
#
_queue_handler: logging.handlers.QueueHandler | None = None


def get_counters() -> dict:
    """Snapshot the publish / drop counters for metrics export."""
    with _counter_lock:
        return {
            "published": _published_count,
            "dropped_queue_full": _dropped_queue_full,
            "dropped_buffer_full": _dropped_buffer_full,
            "dropped_producer_error": _dropped_producer_error,
        }


def _bump(name: str) -> None:
    global _published_count, _dropped_queue_full
    global _dropped_buffer_full, _dropped_producer_error
    with _counter_lock:
        if name == "published":
            _published_count += 1
        elif name == "dropped_queue_full":
            _dropped_queue_full += 1
        elif name == "dropped_buffer_full":
            _dropped_buffer_full += 1
        elif name == "dropped_producer_error":
            _dropped_producer_error += 1


class KafkaHandler(logging.Handler):
    """Logging handler that publishes formatted records to a Kafka topic.

    Designed to be wrapped by ``QueueHandler`` so the synchronous logger
    call returns immediately. The ``emit`` method runs on the
    QueueListener's background thread and blocks while the producer
    enqueues into its own internal buffer; producer-internal flushing
    happens asynchronously.

    Failure modes (broker down, DNS flake, partition unavailable) all
    funnel through ``_dropped_producer_error`` — never raised, never
    blocking the listener thread for long.
    """

    def __init__(self, producer, topic: str):
        super().__init__()
        self._producer = producer
        self._topic = topic

    def emit(self, record: logging.LogRecord) -> None:
        try:
            value = self.format(record).encode("utf-8")
            # No key → round-robin partitioning. See module docstring on
            # multi-producer topology for why we don't key by anything.
            self._producer.produce(self._topic, value=value)
            # Non-blocking poll(0) lets the producer's background thread
            # service delivery callbacks; without this, callbacks queue
            # indefinitely and the producer's internal queue fills.
            self._producer.poll(0)
            _bump("published")
        except BufferError:
            # confluent-kafka's local producer queue full. The producer
            # has its own internal buffer; this fires when *that* fills,
            # independent of our QueueHandler queue. Separate counter so
            # operators can tell "broker is slow / unreachable" (this) from
            # "broker rejected our records" (below).
            _bump("dropped_buffer_full")
        except Exception:
            # Any other producer error — broker down, auth failure,
            # serialization issue. Never raise from a log handler.
            _bump("dropped_producer_error")


def maybe_attach(
    settings,
    formatter: logging.Formatter,
    deployment_name: str = "",
) -> None:
    """Wire a queue-buffered KafkaHandler onto the root logger when
    ``KAFKA_BROKERS`` is set; no-op otherwise.

    Lazy-imports the producer client so the optional ``[kafka]`` extra is
    only required when Kafka is actually enabled. Idempotent — a second
    call is a no-op so test harnesses and accidental double-init don't
    leak listener threads.
    """
    if not settings.kafka.brokers:
        return

    global _listener, _producer, _queue_handler
    if _listener is not None:
        # Already attached. setup_logging() may have replaced
        # logging.root.handlers wholesale just before calling us — if
        # the queue handler was wiped, re-attach it so records keep
        # flowing through the live producer/listener.
        if _queue_handler is not None and _queue_handler not in logging.root.handlers:
            logging.root.addHandler(_queue_handler)
        return

    try:
        from confluent_kafka import Producer
    except ImportError as exc:
        raise RuntimeError(
            "KAFKA_BROKERS is set but confluent-kafka is not installed. "
            "Install with: pip install -e './core[kafka]'"
        ) from exc

    client_id = settings.kafka.client_id or _default_client_id(settings.app_name, deployment_name)
    producer = Producer(
        {
            "bootstrap.servers": settings.kafka.brokers,
            "client.id": client_id,
            "acks": settings.kafka.acks,
        }
    )

    kafka_handler = KafkaHandler(producer, settings.kafka.topic)
    kafka_handler.setFormatter(formatter)

    log_queue: queue.Queue = queue.Queue(maxsize=settings.kafka.queue_max)
    queue_handler = _DropOnFullQueueHandler(log_queue)
    listener = logging.handlers.QueueListener(log_queue, kafka_handler)
    listener.start()

    # Boot diagnostic emits BEFORE attaching the queue handler so it
    # lands on stdout only — operators want the "kafka enabled" line in
    # their aggregator, but Kafka consumers don't need it polluting the
    # event stream they're consuming.
    logger.info(
        "Kafka logging enabled (brokers=%s, topic=%s, client.id=%s)",
        settings.kafka.brokers,
        settings.kafka.topic,
        client_id,
    )

    logging.root.addHandler(queue_handler)

    # Stash listener, producer, and the queue handler so they aren't GC'd
    # and so a re-init can re-attach the handler without rebuilding the
    # stack. stop() flushes producer + listener; process exits → daemon
    # thread tears down naturally.
    _listener = listener
    _producer = producer
    _queue_handler = queue_handler


def stop(flush_timeout: float = 5.0) -> None:
    """Drain the queue, flush the producer, release the listener thread.

    Two-step shutdown: ``QueueListener.stop`` blocks until the in-process
    queue is empty and the listener thread exits, then ``producer.flush``
    blocks until the producer's internal buffer drains (or the timeout
    elapses). Without the flush, records that were enqueued with the
    producer but not yet acked by the broker would be lost.

    Used by integration tests to ensure all records reach the broker
    before the consumer drains. In production, process exit relies on
    daemon-thread teardown; calling this on a graceful shutdown path
    is a future-friendly hook but not required today.
    """
    global _listener, _producer, _queue_handler
    if _listener is not None:
        _listener.stop()
        _listener = None
    if _producer is not None:
        _producer.flush(flush_timeout)
        _producer = None
    _queue_handler = None


class _DropOnFullQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler variant that drops records (and increments a counter)
    when the queue is full instead of blocking on ``queue.put``.

    Standard QueueHandler calls ``self.queue.put_nowait`` which raises
    ``queue.Full`` on overflow; the parent then falls back to ``put``
    which blocks. We override to count + drop instead — sustained log
    pressure shouldn't backpressure into request handlers."""

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            _bump("dropped_queue_full")


def _default_client_id(app_name: str, deployment_name: str) -> str:
    """Derive a per-pod-unique client.id for broker-side observability.

    With N replicas of one deployment streaming to one topic, the broker
    needs to distinguish them. ``<app>-<deployment>-<host>`` does that
    naturally; ``<app>-<host>`` is the fallback when DEPLOYMENT_NAME is
    unset. ``app_name`` is operator-configurable. See the
    multi-producer topology section.
    """
    host = socket.gethostname()
    if deployment_name:
        return f"{app_name}-{deployment_name}-{host}"
    return f"{app_name}-{host}"
