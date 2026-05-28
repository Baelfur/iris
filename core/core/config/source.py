"""Where the service reads ``validation/`` and ``queries/`` YAMLs from.

A ``ConfigSource`` materializes the YAMLs into a directory the existing
loaders (:mod:`core.view_defs`, :mod:`core.custom_queries`)
can read. Three impls:

- :class:`LocalDirSource` — reads from the variant's working directory
  (today's behavior). Default; preserves baked-in-image deployments.
- :class:`GitSource` — clones an external repo on startup. Operators
  who don't want to rebuild the image to change a YAML PR to a config
  repo instead.
- ``DbSource`` (separate issue) — reads from a config Postgres.

Selection via ``CONFIG_SOURCE`` env var. Each source produces a
filesystem path containing ``validation/`` and ``queries/`` subdirs;
the loaders don't care where the bytes came from.

Reload semantics
----------------

``/admin/reload-config`` calls :meth:`ConfigSource.reload` to refresh
the materialized files (git: pull; local: no-op), then re-runs the
loaders. Routes that need updated config don't restart pods.
"""

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfigSource:
    """Base class. Subclasses implement materialize/reload."""

    def materialize(self) -> Path:
        """Return the directory containing ``validation/`` and ``queries/``.

        Idempotent on repeated calls — sources cache the materialized
        location after the first call.
        """
        raise NotImplementedError

    def reload(self) -> Path:
        """Refresh and return the directory. For ``local`` this is a
        no-op; for ``git`` it pulls the configured branch.
        """
        raise NotImplementedError


class LocalDirSource(ConfigSource):
    """Reads YAMLs from a fixed directory (default: the working directory
    where ``validation/`` and ``queries/`` were COPYed into the image).

    No clone, no fetch — just point at what's already on disk. This is the
    default and matches today's behavior bit-for-bit.
    """

    def __init__(self, root: str = "."):
        self.root = Path(root)

    def materialize(self) -> Path:
        """Return the on-disk root; nothing to clone or fetch."""
        return self.root

    def reload(self) -> Path:
        """No-op refresh — loaders re-read from disk on every call."""
        return self.root


class GitSource(ConfigSource):
    """Clones an external git repo on first ``materialize()``; pulls on
    ``reload()``. Uses ``dulwich`` (pure-Python, no system ``git`` binary
    required in the image).

    The repo's tree shape mirrors the in-image layout:

    .. code-block::

        config-repo/
          validation/
            public/
              products.yaml
          queries/
            reports/
              products_by_category.yaml

    Auth options:

    - HTTPS + PAT: ``CONFIG__GIT_TOKEN`` is injected into the URL as
      ``oauth2:<token>``. Works with GitHub, GitLab, Bitbucket.
    - HTTPS public: leave the token unset.
    - SSH: dulwich respects ``ssh://`` URLs; bring your own deploy key
      mounted at the standard ``~/.ssh/`` path.
    """

    def __init__(self, url: str, branch: str = "main", token: str = ""):
        self.url = url
        self.branch = branch
        self.token = token
        self._tmpdir: Path | None = None

    def _auth_url(self) -> str:
        """If a token is configured and the URL is HTTPS, splice in
        Basic auth. Other URL schemes pass through untouched."""
        if not self.token or not self.url.startswith("https://"):
            return self.url
        scheme, rest = self.url.split("://", 1)
        return f"{scheme}://oauth2:{self.token}@{rest}"

    def materialize(self) -> Path:
        """Clone the configured git repo (shallow, single branch) into a
        temp directory on first call; return the cached path thereafter.

        Lazy-imports dulwich so the optional ``[config-git]`` extra is
        only required when ``CONFIG__SOURCE=git`` is actually selected.
        """
        if self._tmpdir is not None:
            return self._tmpdir
        try:
            from dulwich import porcelain
        except ImportError as exc:
            raise RuntimeError(
                "CONFIG__SOURCE=git but dulwich is not installed. "
                "Install with: pip install -e './core[config-git]'"
            ) from exc

        target = Path(tempfile.mkdtemp(prefix="app-config-"))
        logger.info(
            "Cloning config repo %s (branch=%s) → %s",
            self.url,
            self.branch,
            target,
        )
        # porcelain.clone returns a Repo object whose ObjectStore holds
        # open file handles to pack files. We're done with the repo
        # object (we read the cloned tree via Path), so close the handles
        # explicitly to avoid ResourceWarning at GC time.
        repo = porcelain.clone(
            self._auth_url(),
            target=str(target),
            branch=self.branch.encode(),
            depth=1,
        )
        repo.close()
        self._tmpdir = target
        return target

    def reload(self) -> Path:
        """Pull the latest commits on the configured branch into the
        cached clone. Falls back to :meth:`materialize` if the clone
        hasn't happened yet (e.g., admin refresh before first request).
        """
        if self._tmpdir is None:
            return self.materialize()
        from dulwich import porcelain

        logger.info("Pulling config repo updates into %s", self._tmpdir)
        # porcelain.pull(path, ...) opens the repo internally and tears
        # it down on return. Not a perfect cleanup (dulwich's ObjectStore
        # can still leak a pack handle in some versions), but the temp
        # dir is short-lived and removed on process exit, so any leftover
        # is bounded.
        porcelain.pull(
            str(self._tmpdir),
            self._auth_url(),
            refspecs=[f"refs/heads/{self.branch}".encode()],
        )
        return self._tmpdir


def from_settings(settings) -> ConfigSource:
    """Construct the right ``ConfigSource`` from settings.

    Each source's required env vars are validated lazily — bad settings
    fail at lifespan startup with a clear message rather than at the
    first request.
    """
    source = settings.config.source
    if source == "local":
        return LocalDirSource(root=settings.config.local_root)
    if source == "git":
        if not settings.config.git_url:
            raise RuntimeError("CONFIG__SOURCE=git requires CONFIG__GIT_URL to be set")
        return GitSource(
            url=settings.config.git_url,
            branch=settings.config.git_branch or "main",
            token=settings.config.git_token,
        )
    if source == "db":
        # Lazy-import so default-local deployments don't need psycopg.
        from .source_db import DbSource

        return DbSource(
            dsn=settings.config.db_dsn,
            deployment_name=settings.deployment_name,
            app_name=settings.app_name,
        )
    raise RuntimeError(f"Unknown CONFIG__SOURCE: {source!r}")
