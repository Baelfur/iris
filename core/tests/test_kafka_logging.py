"""Tests for core.kafka_logging.

Mock-based — no real Kafka broker required. Real-Kafka format-validation
lives in test-infra (filed as a follow-up issue). Covers the contracts
that matter for unit-level correctness:

- KafkaHandler emits formatted bytes via producer.produce
- Producer errors don't crash the service — they bump the dropped counter
- Queue-overflow path drops + counts (doesn't block)
- Default client.id encodes deployment + host for multi-producer demux
- Settings validator rejects unknown KAFKA_ACKS values
"""

import logging
import queue
import socket
from unittest.mock import MagicMock

import pytest

from core.observability import kafka_logging
from core.observability.kafka_logging import (
    KafkaHandler,
    _default_client_id,
    _DropOnFullQueueHandler,
    get_counters,
)


@pytest.fixture(autouse=True)
def _reset_counters():
    """Each test starts with zeroed counters so assertions are absolute."""
    kafka_logging._published_count = 0
    kafka_logging._dropped_queue_full = 0
    kafka_logging._dropped_buffer_full = 0
    kafka_logging._dropped_producer_error = 0
    yield
    kafka_logging.stop()


def _record(msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


class TestKafkaHandlerEmit:
    def test_publishes_formatted_bytes_with_no_key(self):
        producer = MagicMock()
        handler = KafkaHandler(producer, topic="app.events")
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(_record("hello world"))

        producer.produce.assert_called_once()
        kwargs = producer.produce.call_args.kwargs
        args = producer.produce.call_args.args
        # First positional is topic; value is the encoded message.
        assert args[0] == "app.events"
        assert kwargs["value"] == b"hello world"
        # Keyless: round-robin partitioning, multi-producer-safe.
        assert "key" not in kwargs

    def test_calls_poll_after_produce(self):
        """Without poll(0), confluent-kafka delivery callbacks queue
        unbounded and the producer's internal buffer fills."""
        producer = MagicMock()
        handler = KafkaHandler(producer, topic="x")
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(_record())

        producer.poll.assert_called_once_with(0)

    def test_published_counter_bumps_on_success(self):
        producer = MagicMock()
        handler = KafkaHandler(producer, topic="x")
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(_record())
        handler.emit(_record())

        assert get_counters()["published"] == 2

    def test_buffer_error_bumps_buffer_full_counter(self):
        """confluent-kafka's local producer queue full → BufferError.
        Separate counter from generic producer errors so operators can
        tell 'broker slow' from 'broker rejected'."""
        producer = MagicMock()
        producer.produce.side_effect = BufferError("local queue full")
        handler = KafkaHandler(producer, topic="x")
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(_record())

        c = get_counters()
        assert c["dropped_buffer_full"] == 1
        assert c["dropped_producer_error"] == 0
        assert c["published"] == 0

    def test_arbitrary_producer_exception_drops_and_counts_no_raise(self):
        """Broker down, auth fail, DNS flake — anything from the
        producer client must not propagate from emit()."""
        producer = MagicMock()
        producer.produce.side_effect = RuntimeError("broker unreachable")
        handler = KafkaHandler(producer, topic="x")
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(_record())

        assert get_counters()["dropped_producer_error"] == 1


class TestQueueOverflow:
    def test_full_queue_drops_and_counts(self):
        """The QueueHandler subclass drops on full instead of blocking."""
        q: queue.Queue = queue.Queue(maxsize=2)
        qh = _DropOnFullQueueHandler(q)

        qh.enqueue(_record())
        qh.enqueue(_record())
        # Third record should drop, not block.
        qh.enqueue(_record())

        assert get_counters()["dropped_queue_full"] == 1
        assert q.qsize() == 2


class TestDefaultClientId:
    def test_with_deployment_name(self):
        """Per-pod-unique: <app>-<deployment>-<host>. Multi-producer
        topology requires this — broker-side observability conflates
        replicas otherwise. ``app_name`` is operator-configurable
        (#262)."""
        cid = _default_client_id("app", "inventory")
        assert cid == f"app-inventory-{socket.gethostname()}"

    def test_with_branded_app_name(self):
        cid = _default_client_id("resource-direct", "inventory")
        assert cid == f"resource-direct-inventory-{socket.gethostname()}"

    def test_without_deployment_name(self):
        """Fallback when DEPLOYMENT_NAME is unset — <app>-<host>
        suffices for replica-level uniqueness."""
        cid = _default_client_id("app", "")
        assert cid == f"app-{socket.gethostname()}"


class TestKafkaAcksValidator:
    """Field validator on AppSettings.kafka.acks. (#23)"""

    def test_accepts_valid_values(self, monkeypatch):
        from core.config.settings import AppSettings

        class _S(AppSettings):
            pass

        monkeypatch.setenv("CONFIG__SOURCE", "local")
        monkeypatch.setenv("AUTH__MODE", "gateway")
        for v in ("0", "1", "all"):
            monkeypatch.setenv("KAFKA__ACKS", v)
            assert _S().kafka.acks == v

    def test_rejects_unknown(self, monkeypatch):
        from core.config.settings import AppSettings

        class _S(AppSettings):
            pass

        monkeypatch.setenv("CONFIG__SOURCE", "local")
        monkeypatch.setenv("AUTH__MODE", "gateway")
        monkeypatch.setenv("KAFKA__ACKS", "two")
        with pytest.raises(ValueError, match="KAFKA__ACKS"):
            _S()


class TestMaybeAttach:
    """KAFKA_BROKERS unset → maybe_attach is a no-op. Default-off means
    default-off; no extra thread, no producer import."""

    @staticmethod
    def _kafka_settings(**fields):
        """Build a stand-in settings object with the kafka submodel populated."""
        class _Kafka:
            brokers = fields.get("brokers", "")
            topic = fields.get("topic", "x")
            client_id = fields.get("client_id", "")
            acks = fields.get("acks", "1")
            queue_max = fields.get("queue_max", 10)

        class _S:
            kafka = _Kafka()

        return _S()

    def test_noop_when_brokers_empty(self):
        kafka_logging.maybe_attach(
            settings=self._kafka_settings(brokers=""),
            formatter=logging.Formatter(),
        )
        assert kafka_logging._listener is None

    def test_double_attach_is_idempotent(self, monkeypatch):
        """A second call returns early rather than spawning another
        listener thread or registering a duplicate handler."""
        # Pretend a listener is already in place.
        sentinel = object()
        monkeypatch.setattr(kafka_logging, "_listener", sentinel)

        kafka_logging.maybe_attach(
            settings=self._kafka_settings(brokers="broker:9092"),
            formatter=logging.Formatter(),
        )
        # Listener untouched — second call short-circuited.
        assert kafka_logging._listener is sentinel

    def test_reinit_reattaches_queue_handler(self, monkeypatch):
        """When setup_logging() runs a second time it wipes
        logging.root.handlers wholesale, dropping the queue handler.
        maybe_attach() must re-attach the existing handler so records
        keep flowing through the live producer/listener. (#127)"""
        # Pretend a previous attach already happened: stash a sentinel
        # listener and a real (un-attached) QueueHandler instance.
        sentinel_listener = object()
        import queue as _queue
        log_q: _queue.Queue = _queue.Queue(maxsize=10)
        existing_handler = kafka_logging._DropOnFullQueueHandler(log_q)
        monkeypatch.setattr(kafka_logging, "_listener", sentinel_listener)
        monkeypatch.setattr(kafka_logging, "_queue_handler", existing_handler)

        # Simulate setup_logging wiping root handlers.
        original_handlers = list(logging.root.handlers)
        try:
            logging.root.handlers = [logging.StreamHandler()]
            assert existing_handler not in logging.root.handlers

            kafka_logging.maybe_attach(
                settings=self._kafka_settings(brokers="broker:9092"),
                formatter=logging.Formatter(),
            )

            # Existing handler is re-attached; listener untouched.
            assert existing_handler in logging.root.handlers
            assert kafka_logging._listener is sentinel_listener
        finally:
            logging.root.handlers = original_handlers
