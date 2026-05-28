"""Tests for core.logging_config.JSONFormatter.

The formatter emits one JSON object per log record. ``database`` and
``app`` are always present; ``deployment`` is included only when
``deployment_name`` is set. (#262 added the ``app`` field.)
"""

import json
import logging

from core.observability.logging_config import JSONFormatter


def _record(msg: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )


class TestJSONFormatter:
    def test_database_field_always_present(self):
        formatter = JSONFormatter(database="postgresql")
        out = json.loads(formatter.format(_record()))
        assert out["database"] == "postgresql"
        assert out["message"] == "hello"
        assert out["level"] == "INFO"

    def test_app_field_defaults_to_neutral(self):
        """Default APP_NAME="app" surfaces as the ``app`` field on
        every record. Operators filter / group by ``app`` rather than
        by the per-module ``logger`` name. (#262)"""
        formatter = JSONFormatter(database="postgresql")
        out = json.loads(formatter.format(_record()))
        assert out["app"] == "app"

    def test_app_field_carries_operator_brand(self):
        formatter = JSONFormatter(database="postgresql", app_name="resource-direct")
        out = json.loads(formatter.format(_record()))
        assert out["app"] == "resource-direct"

    def test_host_field_always_present(self):
        """`host` enables multi-producer demux when N service instances
        stream to one Kafka topic. Always emitted, no env var. (#23)"""
        import socket
        formatter = JSONFormatter(database="postgresql")
        out = json.loads(formatter.format(_record()))
        assert out["host"] == socket.gethostname()

    def test_deployment_field_omitted_when_unset(self):
        """Default-off should not pollute log records with an empty
        deployment field — matches the broader 'no surprise output'
        posture."""
        formatter = JSONFormatter(database="postgresql")
        out = json.loads(formatter.format(_record()))
        assert "deployment" not in out

    def test_deployment_field_omitted_when_empty_string(self):
        """Explicit empty string is the same as unset — operators who
        leave DEPLOYMENT_NAME blank shouldn't see a "" deployment label
        in their aggregator."""
        formatter = JSONFormatter(database="postgresql", deployment_name="")
        out = json.loads(formatter.format(_record()))
        assert "deployment" not in out

    def test_deployment_field_present_when_set(self):
        formatter = JSONFormatter(
            database="postgresql", deployment_name="inventory",
        )
        out = json.loads(formatter.format(_record()))
        assert out["deployment"] == "inventory"
        assert out["database"] == "postgresql"

    def test_exception_field_added_when_record_carries_exc_info(self):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname=__file__, lineno=1,
                msg="failed", args=(), exc_info=sys.exc_info(),
            )
        formatter = JSONFormatter(database="postgresql", deployment_name="inv")
        out = json.loads(formatter.format(record))
        assert "exception" in out
        assert "RuntimeError" in out["exception"]
        # Other fields still populated alongside the exception.
        assert out["deployment"] == "inv"


class TestLogLevel:
    """``LOG_LEVEL`` env var (#356) sets the root logger; default INFO
    matches pre-#356 hardcoded behavior. uvicorn.access is pinned to
    WARNING regardless."""

    def teardown_method(self):
        # Reset root so per-test isolation holds — other tests don't
        # want our DEBUG setting bleeding in.
        logging.root.setLevel(logging.WARNING)
        logging.root.handlers = []

    def test_default_is_info(self):
        from core.observability.logging_config import setup_logging

        setup_logging(database="postgresql")
        assert logging.root.level == logging.INFO

    def test_explicit_info_applies(self):
        from core.observability.logging_config import setup_logging

        setup_logging(database="postgresql", log_level="INFO")
        assert logging.root.level == logging.INFO

    def test_debug_applies(self):
        from core.observability.logging_config import setup_logging

        setup_logging(database="postgresql", log_level="DEBUG")
        assert logging.root.level == logging.DEBUG

    def test_warning_quiets_info(self):
        """LOG_LEVEL=WARNING should silence INFO records — confirmed
        via the level on the root logger rather than capturing output."""
        from core.observability.logging_config import setup_logging

        setup_logging(database="postgresql", log_level="WARNING")
        assert logging.root.level == logging.WARNING
        assert not logging.root.isEnabledFor(logging.INFO)
        assert logging.root.isEnabledFor(logging.WARNING)

    def test_case_insensitive(self):
        """Operators using ``LOG_LEVEL=debug`` (lowercase) should not
        get surprised. The setup_logging helper uppercases internally;
        the AppSettings validator also normalizes."""
        from core.observability.logging_config import setup_logging

        setup_logging(database="postgresql", log_level="debug")
        assert logging.root.level == logging.DEBUG

    def test_unknown_level_falls_back_to_info(self):
        """Defensive: an unexpected value (shouldn't happen since
        AppSettings.log_level is a Literal, but the helper is also
        called from tests) defaults to INFO instead of crashing."""
        from core.observability.logging_config import setup_logging

        setup_logging(database="postgresql", log_level="NONSENSE")
        assert logging.root.level == logging.INFO

    def test_uvicorn_access_stays_warning_at_debug(self):
        """LOG_LEVEL=DEBUG shouldn't surface uvicorn.access duplicates.
        The service emits its own request log line in app_meta middleware;
        uvicorn.access is deliberately pinned to WARNING."""
        from core.observability.logging_config import setup_logging

        setup_logging(database="postgresql", log_level="DEBUG")
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
