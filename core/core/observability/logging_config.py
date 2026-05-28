"""Structured JSON logging configuration.

Emits one JSON object per log record. Always includes ``host`` (from
``socket.gethostname()``) so downstream consumers can demux when N the service
producers stream to a single sink — particularly relevant for Kafka
where multi-producer topologies are first-class.
"""

import json
import logging
import socket
import sys
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON for log aggregators / Kafka.

    ``app`` carries the application brand (operator-configurable per
    Filter / group log streams by this field rather than by the
    logger root name — module loggers use ``getLogger(__name__)``, so
    the logger column is per-module noise. ``database`` is always
    present (per-variant tag for filtering). ``host`` is always present
    (pod / container / hostname) — needed for multi-producer demux when
    N instances stream to one Kafka topic. ``deployment`` is included
    only when DEPLOYMENT_NAME is set.
    """

    def __init__(self, database: str, app_name: str = "app", deployment_name: str = ""):
        super().__init__()
        self.database = database
        self.app_name = app_name
        self.deployment_name = deployment_name
        self.host = socket.gethostname()

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "app": self.app_name,
            "database": self.database,
            "host": self.host,
        }
        if self.deployment_name:
            log_entry["deployment"] = self.deployment_name
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def setup_logging(
    database: str,
    app_name: str = "app",
    deployment_name: str = "",
    kafka_settings: object | None = None,
    log_level: str = "INFO",
) -> None:
    """Configure root logger with JSON output to stdout, optionally to Kafka.

    Args:
        database: Database type tag (postgresql, mysql, mariadb, oracle, trino).
        app_name: Application brand identity. Operator-configurable per
            surfaces on every record as the ``app`` field.
        deployment_name: Canonical deployment identity. Cascades from
            ``DEPLOYMENT_NAME``; included in records when set.
        kafka_settings: Optional settings object exposing ``kafka_brokers``
            (and friends). When ``kafka_brokers`` is non-empty, attach a
            queue-buffered Kafka handler emitting the same JSON envelope
            going to stdout. Default off — no Kafka, no extra dep, no
            extra thread.
        log_level: Standard Python logging level name (``DEBUG``,
            ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``). Set on the
            root logger so module loggers inherit. Defaults to
            ``INFO`` — matches previously hardcoded behavior. The
            ``uvicorn.access`` logger is independently pinned to
            ``WARNING`` regardless — it duplicates the service's own
            request log line and is silenced deliberately.
    """
    formatter = JSONFormatter(database=database, app_name=app_name, deployment_name=deployment_name)
    handlers = [logging.StreamHandler(sys.stdout)]
    for h in handlers:
        h.setFormatter(formatter)

    logging.root.handlers = list(handlers)
    logging.root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    if kafka_settings is not None:
        from . import kafka_logging

        kafka_logging.maybe_attach(
            settings=kafka_settings,
            formatter=formatter,
            deployment_name=deployment_name,
        )
