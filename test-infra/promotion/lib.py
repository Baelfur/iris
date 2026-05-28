"""Helpers for the promotion harness.

Builds a PREV image (from a git ref via ``git worktree``) and a CURRENT
image (from the working tree), runs each in a managed container, and
asserts the service contract holds across both. Catches the regression
class "PR broke upgrade-in-place." (#119, #108)

The harness is invoked via pytest:

    PROMOTION_PREV_REF=v1.0.0 pytest test-infra/promotion/

New variants land by appending to ``VARIANTS`` below — sibling subs
(#120-#123) collapse into a fixture-list addition rather than a copied
shell-script directory.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Variant:
    """One variant's promotion-test configuration."""

    name: str
    dockerfile: str   # relative to REPO_ROOT, e.g. "variants/postgres/Dockerfile"
    port: int         # host port for the running container
    env: dict[str, str] = field(default_factory=dict)
    # Path of the canonical products query for the contract check.
    # ``None`` when test-infra doesn't seed a products table for this
    # variant — currently only trino, whose memory catalog starts
    # empty. For ``None`` variants the contract collapses to /health
    # plus a 404-on-bad-schema probe; the data-plane assertion is
    # skipped because there's nothing to assert against.
    products_path: Optional[str] = "/public/products"


# Per-variant configuration. Ports are +10000 from each variant's
# standard port to avoid collision with anything else the operator has
# running. Each variant's data DB is the test-infra service of the same
# name on the host (postgres, mysql, mariadb, oracle, trino).
VARIANTS: list[Variant] = [
    Variant(
        name="postgres",
        dockerfile="variants/postgres/Dockerfile",
        port=18222,
        env={
            "PG_HOST": "host.docker.internal",
            "PG_PORT": "5432",
            "PG_USER": "postgres",
            "PG_PASSWORD": "testpass",
            "PG_DATABASE": "app",
            "ALLOWED_SCHEMAS": '["public"]',
            "CONFIG__SOURCE": "local",
            "DEPLOYMENT_NAME": "postgres_promotion",
        },
    ),
    Variant(
        name="mysql",
        dockerfile="variants/mysql/Dockerfile",
        port=18221,
        env={
            "MYSQL_HOST": "host.docker.internal",
            "MYSQL_PORT": "3306",
            "MYSQL_USER": "root",
            "MYSQL_PASSWORD": "testpass",
            "MYSQL_DATABASE": "public",
            "ALLOWED_SCHEMAS": '["public"]',
            "CONFIG__SOURCE": "local",
            "DEPLOYMENT_NAME": "mysql_promotion",
        },
    ),
    Variant(
        name="mariadb",
        dockerfile="variants/mariadb/Dockerfile",
        port=18223,
        env={
            "MARIADB_HOST": "host.docker.internal",
            "MARIADB_PORT": "3307",
            "MARIADB_USER": "root",
            "MARIADB_PASSWORD": "testpass",
            "MARIADB_DATABASE": "public",
            "ALLOWED_SCHEMAS": '["public"]',
            "CONFIG__SOURCE": "local",
            "DEPLOYMENT_NAME": "mariadb_promotion",
        },
    ),
    Variant(
        name="oracle",
        dockerfile="variants/oracle/Dockerfile",
        port=18220,
        env={
            "ORACLE_HOST": "host.docker.internal",
            "ORACLE_PORT": "1521",
            "ORACLE_USER": "public_user",
            "ORACLE_PASSWORD": "testpass",
            "ORACLE_SERVICE": "FREEPDB1",
            # Oracle's "schema" is the user; the test-infra setup creates
            # public_user and seeds public_user.products.
            "ALLOWED_SCHEMAS": '["public_user"]',
            "CONFIG__SOURCE": "local",
            "DEPLOYMENT_NAME": "oracle_promotion",
        },
        products_path="/public_user/products",
    ),
    Variant(
        name="trino",
        dockerfile="variants/trino/Dockerfile",
        port=18224,
        env={
            "TRINO_HOST": "host.docker.internal",
            "TRINO_PORT": "8080",
            "TRINO_USER": "app",
            "TRINO_CATALOG": "memory",
            "TRINO_SCHEME": "http",
            "CONFIG__SOURCE": "local",
            "DEPLOYMENT_NAME": "trino_promotion",
            # No ALLOWED_SCHEMAS — empty allowlist + harvest non-system
            # schemas (post-#64 default). The memory catalog starts
            # empty in test-infra, so the contract collapses to /health
            # + 404 probe; see products_path=None below.
        },
        products_path=None,
    ),
]


# ---------------------------------------------------------------- image building


def _short_sha(ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", ref],
        cwd=REPO_ROOT, capture_output=True, check=True, text=True,
    ).stdout.strip()


def _image_exists(tag: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    ).returncode == 0


def _build(tag: str, dockerfile: Path, context: Path) -> None:
    subprocess.run(
        ["docker", "build", "-t", tag, "-f", str(dockerfile), str(context)],
        check=True, capture_output=True,
    )


def build_prev_image(variant: Variant, prev_ref: str) -> str:
    """Build (or reuse) the PREV image for ``variant`` at ``prev_ref``.

    Tags by short SHA so re-runs against the same ref skip the build —
    common during scenario-development iteration. The git worktree is
    created in a temp dir and removed on completion; the working tree
    is untouched.
    """
    sha = _short_sha(prev_ref)
    tag = f"app-{variant.name}:promotion-prev-{sha}"
    if _image_exists(tag):
        return tag

    with tempfile.TemporaryDirectory(prefix="app-promotion-prev-") as worktree:
        subprocess.run(
            ["git", "worktree", "add", "--detach", worktree, prev_ref],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        try:
            _build(
                tag,
                dockerfile=Path(worktree) / variant.dockerfile,
                context=Path(worktree),
            )
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree],
                cwd=REPO_ROOT, capture_output=True,
            )
    return tag


def build_current_image(variant: Variant) -> str:
    """Build CURRENT from the working tree. Always rebuilds — the
    working tree is what's under test, so caching by SHA would defeat
    the purpose during PR iteration."""
    tag = f"app-{variant.name}:promotion-current"
    _build(
        tag,
        dockerfile=REPO_ROOT / variant.dockerfile,
        context=REPO_ROOT,
    )
    return tag


# ----------------------------------------------------------- container lifecycle


@contextmanager
def running_container(
    image: str, port: int, env: dict[str, str], ready_timeout: float = 60.0,
) -> Iterator[str]:
    """Start a container, wait for /health, yield its host base URL,
    teardown on exit.

    ``host.docker.internal`` is added to /etc/hosts inside the container
    via ``--add-host`` — Linux Docker (where it's not native) needs this
    explicitly; macOS Docker Desktop ignores it.
    """
    cmd = [
        "docker", "run", "-d",
        "-p", f"{port}:8000",
        "--add-host", "host.docker.internal:host-gateway",
    ]
    for k, v in env.items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.append(image)

    cid = subprocess.run(
        cmd, capture_output=True, check=True, text=True,
    ).stdout.strip()

    url = f"http://localhost:{port}"
    try:
        _wait_for_ready(url, timeout=ready_timeout, container_id=cid)
        yield url
    finally:
        subprocess.run(["docker", "stop", cid], capture_output=True)
        subprocess.run(["docker", "rm", cid], capture_output=True)


def _wait_for_ready(url: str, timeout: float, container_id: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    # Capture the container's logs to make the failure diagnosable
    # rather than leaving the operator to find them by hand.
    logs = subprocess.run(
        ["docker", "logs", "--tail", "50", container_id],
        capture_output=True, text=True,
    )
    raise TimeoutError(
        f"{url} did not become ready within {timeout:.0f}s.\n"
        f"--- last 50 lines of container logs ---\n"
        f"{logs.stdout}{logs.stderr}"
    )


# ----------------------------------------------------------------- assertions


def assert_contract(base_url: str, variant: Variant, label: str) -> None:
    """Stable-subset contract assertions. Adding new fields in CURRENT
    doesn't fail this — only changing or removing a contract field does.
    That's the actual forward-compat contract the service makes.
    """
    health = httpx.get(f"{base_url}/health", timeout=5.0).json()
    assert health.get("status") == "ok", \
        f"[{label}] /health did not return status=ok: {health!r}"

    if variant.products_path is None:
        # Variants without seeded data (currently trino — memory catalog
        # starts empty). Dynamic-surface contract collapses to "bad
        # schema returns 404, not 500."
        bad = httpx.get(f"{base_url}/nope/products", timeout=5.0)
        assert bad.status_code == 404, \
            f"[{label}] /nope/products returned {bad.status_code} (want 404)"
        return

    products_resp = httpx.get(
        f"{base_url}{variant.products_path}",
        params={"id": "1"},
        timeout=10.0,
    )
    assert products_resp.status_code == 200, \
        f"[{label}] {variant.products_path}?id=1 returned {products_resp.status_code}"
    products = products_resp.json()
    assert products.get("name") == "products", \
        f"[{label}] envelope.name != 'products': {products!r}"
    assert any(e.get("id") == 1 for e in products.get("elements", [])), \
        f"[{label}] envelope.elements missing id=1: {products!r}"

    # Validation contract: bad column → 400, never 500.
    bad = httpx.get(
        f"{base_url}{variant.products_path}",
        params={"$select": "fake_col", "id": "1"},
        timeout=5.0,
    )
    assert bad.status_code == 400, \
        f"[{label}] bad-column probe returned {bad.status_code} (want 400)"
