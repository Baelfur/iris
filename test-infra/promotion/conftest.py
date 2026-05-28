"""Promotion-harness fixtures: skip gates + session-scoped image builds.

Skips the whole test set when ``PROMOTION_PREV_REF`` is unset or
Docker isn't reachable — same shape as the existing skip-when-env-unset
pattern in ``core/tests/test_config_source_db.py`` and
``test_kafka_logging_integration.py``.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from .lib import VARIANTS, Variant, build_current_image, build_prev_image


_PREV_REF = os.environ.get("PROMOTION_PREV_REF", "")


@pytest.fixture(scope="session", autouse=True)
def _docker_available() -> None:
    """Skip the whole test session when the Docker daemon is unreachable.
    The harness needs ``docker run`` to do its job; without it every test
    would fail with a confusing CalledProcessError."""
    try:
        subprocess.run(
            ["docker", "version"],
            capture_output=True, check=True, timeout=5,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip(
            "Docker daemon not reachable — promotion harness needs Docker."
        )


@pytest.fixture(scope="session")
def prev_ref() -> str:
    """Required: the git ref to upgrade *from*. No default — operators
    decide what's being upgraded. Skip when unset so the harness doesn't
    pretend to test something."""
    if not _PREV_REF:
        pytest.skip(
            "PROMOTION_PREV_REF unset; promotion tests need a git "
            "ref to upgrade from. Examples: v1.0.0, $(git rev-parse HEAD~5)."
        )
    return _PREV_REF


@pytest.fixture(scope="session")
def prev_images(prev_ref: str) -> dict[str, str]:
    """Build PREV images for every variant once per test session.
    Returns ``{variant_name: image_tag}``. SHA-tagged so re-runs against
    the same PREV_REF skip the rebuild."""
    return {v.name: build_prev_image(v, prev_ref) for v in VARIANTS}


@pytest.fixture(scope="session")
def current_images() -> dict[str, str]:
    """Build CURRENT images for every variant once per session. Always
    rebuilds (the working tree is what's under test)."""
    return {v.name: build_current_image(v) for v in VARIANTS}
