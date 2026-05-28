"""Integration tests — run shared suite against seeded MySQL.

All tests come from :mod:`core.testing.integration_suite`; per-variant
context (URL paths, passthrough credentials, the ASGI client) is provided
by fixtures in ``conftest.py``.
"""

from core.testing.integration_suite import *  # noqa: F401, F403
