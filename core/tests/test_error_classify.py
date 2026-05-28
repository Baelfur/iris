"""Tests for error_classify and db_error_body.

Driver-text → stable code is the contract clients rely on under
ERROR_DETAIL=safe. Patterns are matched case-insensitively; first match
wins; unrecognised text falls through to ``db.query_failed``.
"""

from dataclasses import dataclass

import pytest

from core import context
from core.errors.classify import classify
from core.errors.messages import db_error_body

# --- classifier ---

@pytest.mark.parametrize("detail, expected_code", [
    # auth
    ("FATAL: password authentication failed for user \"alice\"", "db.bad_credentials"),
    ("Access denied for user 'alice'@'10.0.0.1' (using password: YES)", "db.bad_credentials"),
    ("ORA-01017: invalid username/password; logon denied", "db.bad_credentials"),
    ("Authentication failed: bad password", "db.bad_credentials"),
    # reachability
    ("could not connect to server: Connection refused", "db.connection_refused"),
    ("connection refused", "db.connection_refused"),
    ("ORA-12170: TNS:Connect timeout occurred", "db.connection_refused"),
    ("Can't connect to MySQL server on 'db.internal' (10061)", "db.connection_refused"),
    # authz
    ("permission denied for table customers", "db.permission_denied"),
    ("ORA-00942: table or view does not exist", "db.permission_denied"),
    ("ORA-01031: insufficient privileges", "db.permission_denied"),
    ("SELECT command denied to user 'alice'@'host' for table 'audit'", "db.permission_denied"),
    # timeouts
    ("canceling statement due to statement timeout", "db.timeout"),
    ("Query execution was interrupted, max_statement_time exceeded", "db.timeout"),
    ("max_execution_time exceeded", "db.timeout"),
    ("ORA-01013: user requested cancel of current operation", "db.timeout"),
    ("Query exceeded the maximum execution time of 30 seconds", "db.timeout"),
    # fallback
    ("syntax error at or near \"FROM\"", "db.query_failed"),
    ("", "db.query_failed"),
    ("some completely unrelated text", "db.query_failed"),
])
def test_classify_returns_expected_code(detail, expected_code):
    code, message = classify(detail)
    assert code == expected_code
    assert message  # non-empty human message
    # Stable contract: codes namespaced under db.
    assert code.startswith("db.")


def test_classify_case_insensitive():
    code, _ = classify("PASSWORD AUTHENTICATION FAILED FOR USER")
    assert code == "db.bad_credentials"


def test_classify_first_match_wins():
    # Both "permission denied" and "statement timeout" patterns present;
    # permission_denied appears first in the table → wins.
    code, _ = classify("permission denied for table; statement timeout fired")
    assert code == "db.permission_denied"


def test_classify_message_contains_no_input_text():
    """Safe-mode contract: messages must not echo driver text back to caller."""
    leaky = "permission denied for table audit_log on host db-prod-01"
    _, message = classify(leaky)
    assert "audit_log" not in message
    assert "db-prod-01" not in message


@pytest.mark.parametrize("detail", [
    # Identifier-as-substring cases that pre-#136 would have mis-classified
    # because simple ``in`` substring matching ignored word boundaries.
    'ERROR: column "my_max_statement_time_value" does not exist',
    'ERROR: column "tracked_max_execution_time_ms" does not exist',
    "ERROR: relation \"audit_max_statement_time\" does not exist",
])
def test_classify_word_boundary_avoids_identifier_collisions(detail):
    """Underscore-bearing identifiers that contain a pattern phrase should
    not trigger the pattern's classification — the pattern phrase needs a
    real word boundary, not a position inside a snake_case identifier. (#136)"""
    code, _ = classify(detail)
    assert code == "db.query_failed", \
        f"identifier-collision case {detail!r} mis-classified as {code}"


def test_classify_still_matches_legitimate_timeout():
    """Sanity check that the word-boundary tightening didn't break the
    real ``max_statement_time`` driver text used by MySQL."""
    code, _ = classify(
        "Query execution was interrupted, max_statement_time exceeded"
    )
    assert code == "db.timeout"


# --- db_error_body integration with ERROR_DETAIL ---

@dataclass
class _Settings:
    error_detail: str = "terse"
    deployment_name: str = ""


def _set_mode(mode: str, deployment_name: str = "", database: str = "test"):
    s = _Settings(error_detail=mode, deployment_name=deployment_name)
    context.set_context(context.AppContext(
        fetch_all=None, harvest_ddl=None, paramstyle="pyformat",
        settings=s, database=database,
    ))


class TestDbErrorBody:
    def teardown_method(self):
        context._ctx = None

    def test_terse_returns_generic_envelope(self):
        _set_mode("terse")
        body = db_error_body("password authentication failed for user alice")
        assert body == {
            "error": {"code": "db.query_failed", "message": "Query failed"}
        }

    def test_verbose_returns_raw_detail(self):
        _set_mode("verbose")
        leaky = "FATAL: password authentication failed for user \"alice\""
        body = db_error_body(leaky)
        # Verbose envelope: classify supplies the code, raw text is the
        # message, debug context is sibling fields (#101).
        assert body["error"]["code"] == "db.bad_credentials"
        assert body["error"]["message"] == leaky

    def test_safe_returns_code_and_message(self):
        _set_mode("safe")
        body = db_error_body("FATAL: password authentication failed for user \"alice\"")
        assert body == {
            "error": {
                "code": "db.bad_credentials",
                "message": "Database rejected the supplied credentials",
            }
        }

    def test_safe_falls_through_to_query_failed(self):
        _set_mode("safe")
        body = db_error_body("syntax error at or near \"SELECT\"")
        assert body["error"]["code"] == "db.query_failed"

    def test_safe_does_not_leak_input_text(self):
        _set_mode("safe")
        leaky = "permission denied for table customers on host db-prod-01"
        body = db_error_body(leaky)
        assert "customers" not in str(body)
        assert "db-prod-01" not in str(body)

    # --- verbose-mode operator-debug context (#101) ---

    def test_verbose_includes_database_field(self):
        _set_mode("verbose", database="postgresql")
        body = db_error_body("FATAL: something")
        assert body["database"] == "postgresql"
        assert body["error"]["message"] == "FATAL: something"

    def test_verbose_includes_deployment_when_set(self):
        _set_mode("verbose", deployment_name="inventory", database="mysql")
        body = db_error_body("FATAL: something")
        assert body["deployment"] == "inventory"
        assert body["database"] == "mysql"

    def test_verbose_omits_deployment_when_unset(self):
        _set_mode("verbose", database="mariadb")
        body = db_error_body("FATAL: something")
        assert "deployment" not in body
        assert body["database"] == "mariadb"

    def test_terse_does_not_leak_database(self):
        _set_mode("terse", deployment_name="inventory", database="oracle")
        body = db_error_body("ORA-01017: invalid credentials")
        assert body == {
            "error": {"code": "db.query_failed", "message": "Query failed"}
        }
        assert "deployment" not in body
        assert "database" not in body

    def test_safe_does_not_leak_deployment_or_database(self):
        """Safe mode preserves stable shape — no debug fields. The
        X-{App}-Deployment response header (#97) carries deployment for
        machine consumers without polluting the body contract."""
        _set_mode("safe", deployment_name="inventory", database="postgresql")
        body = db_error_body("FATAL: password authentication failed")
        assert "deployment" not in body
        assert "database" not in body
        assert body == {
            "error": {
                "code": "db.bad_credentials",
                "message": "Database rejected the supplied credentials",
            }
        }
