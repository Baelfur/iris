"""Tests for the container entrypoint Python script.

The script is launcher-time logic: it reads ``TLS_CERT_FILE`` and
``TLS_KEY_FILE`` from the environment and adds ``--ssl-*`` flags to the
uvicorn invocation when both are set. ``DRY_RUN=1`` makes it print
the command instead of execing, which is what we use here.

Python (not shell) so the entrypoint runs on shell-less hardened base
images (#169).
"""

import subprocess
import sys
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[2] / "scripts" / "entrypoint.py"


def _run(env_overrides: dict) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "DRY_RUN": "1", **env_overrides}
    return subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_no_tls_env_launches_http():
    """Without TLS_CERT_FILE/TLS_KEY_FILE, no --ssl-* flags are added."""
    result = _run({})
    assert result.returncode == 0, result.stderr
    assert "uvicorn app.main:app" in result.stdout
    assert "--host 0.0.0.0" in result.stdout
    assert "--port 8000" in result.stdout
    assert "--ssl-certfile" not in result.stdout
    assert "--ssl-keyfile" not in result.stdout


def test_both_tls_env_adds_ssl_flags():
    """Both TLS_CERT_FILE and TLS_KEY_FILE → uvicorn gets --ssl-* flags."""
    result = _run({
        "TLS_CERT_FILE": "/etc/tls/cert.pem",
        "TLS_KEY_FILE": "/etc/tls/key.pem",
    })
    assert result.returncode == 0, result.stderr
    assert "--ssl-certfile=/etc/tls/cert.pem" in result.stdout
    assert "--ssl-keyfile=/etc/tls/key.pem" in result.stdout


def test_only_cert_set_is_rejected():
    """Half-configured TLS is a misconfiguration, not a silent fall-back to HTTP."""
    result = _run({"TLS_CERT_FILE": "/etc/tls/cert.pem"})
    assert result.returncode != 0
    assert "must both be set" in result.stderr


def test_only_key_set_is_rejected():
    result = _run({"TLS_KEY_FILE": "/etc/tls/key.pem"})
    assert result.returncode != 0
    assert "must both be set" in result.stderr
