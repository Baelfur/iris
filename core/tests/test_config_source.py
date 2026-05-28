"""Tests for core.config.source.

Unit-level coverage of the source dispatch + GitSource auth-URL logic.
The actual git clone path is mock-based so the test doesn't need a real
remote or the dulwich extra installed. (#98)
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.config.source import (
    ConfigSource,
    GitSource,
    LocalDirSource,
    from_settings,
)


def _settings(**kwargs) -> SimpleNamespace:
    """Build a stand-in settings object with the config submodel populated.

    Tests pass nested-style kwargs (``source="local"``, ``git_url=...``);
    the helper places them under the ``config`` submodel that
    ``from_settings`` reads. ``deployment_name`` stays flat.
    """
    config_defaults = {
        "source": "local",
        "local_root": ".",
        "git_url": "",
        "git_branch": "main",
        "git_token": "",
        "db_dsn": "",
    }
    deployment_name = kwargs.pop("deployment_name", "")
    app_name = kwargs.pop("app_name", "app")
    config_defaults.update(kwargs)
    return SimpleNamespace(
        config=SimpleNamespace(**config_defaults),
        deployment_name=deployment_name,
        app_name=app_name,
    )


class TestLocalDirSource:
    def test_materialize_returns_configured_root(self):
        s = LocalDirSource(root="/some/path")
        assert s.materialize() == Path("/some/path")

    def test_default_root_is_current_dir(self):
        s = LocalDirSource()
        assert s.materialize() == Path(".")

    def test_reload_is_noop(self):
        """local source has nothing to refresh — disk is the source."""
        s = LocalDirSource(root="/some/path")
        assert s.reload() == Path("/some/path")


class TestGitSourceAuthURL:
    """Token injection for HTTPS only — SSH and other schemes pass
    through untouched."""

    def test_no_token_passes_url_through(self):
        s = GitSource(url="https://example.com/r.git", branch="main", token="")
        assert s._auth_url() == "https://example.com/r.git"

    def test_https_token_injected_as_oauth2(self):
        s = GitSource(url="https://example.com/r.git", branch="main", token="abc")
        assert s._auth_url() == "https://oauth2:abc@example.com/r.git"

    def test_ssh_url_passes_through_even_with_token(self):
        """Token is HTTPS-Basic-auth-shaped; not relevant for SSH. Don't
        molest the URL."""
        s = GitSource(url="ssh://git@example.com/r.git", branch="main", token="abc")
        assert s._auth_url() == "ssh://git@example.com/r.git"

    def test_http_passes_through(self):
        """Plain HTTP isn't given the token treatment either — keep
        token injection scoped to HTTPS to avoid leaking it over the
        wire. Operators using plain HTTP shouldn't have tokens anyway."""
        s = GitSource(url="http://example.com/r.git", branch="main", token="abc")
        assert s._auth_url() == "http://example.com/r.git"


class TestGitSourceMissingDependency:
    def test_helpful_error_when_dulwich_not_installed(self):
        """Lazy import in materialize → clear install hint, not
        ImportError."""
        s = GitSource(url="https://example.com/r.git", branch="main")
        with patch.dict(sys.modules, {"dulwich": None, "dulwich.porcelain": None}):
            with pytest.raises(RuntimeError, match=r"core\[config-git\]"):
                s.materialize()


class TestGitSourceCloneAndPull:
    """End-to-end clone + pull with dulwich mocked. We're verifying the
    call shape, not exercising real git plumbing."""

    def test_first_materialize_clones(self):
        fake_porcelain = MagicMock()
        with patch.dict(sys.modules, {
            "dulwich": MagicMock(porcelain=fake_porcelain),
            "dulwich.porcelain": fake_porcelain,
        }):
            s = GitSource(url="https://example.com/r.git", branch="main", token="t")
            target = s.materialize()
        assert isinstance(target, Path)
        # Called exactly once with the auth-injected URL + branch + shallow.
        fake_porcelain.clone.assert_called_once()
        kwargs = fake_porcelain.clone.call_args.kwargs
        args = fake_porcelain.clone.call_args.args
        assert args[0] == "https://oauth2:t@example.com/r.git"
        assert kwargs["branch"] == b"main"
        assert kwargs["depth"] == 1

    def test_second_materialize_does_not_re_clone(self):
        fake_porcelain = MagicMock()
        with patch.dict(sys.modules, {
            "dulwich": MagicMock(porcelain=fake_porcelain),
            "dulwich.porcelain": fake_porcelain,
        }):
            s = GitSource(url="https://example.com/r.git", branch="main")
            first = s.materialize()
            second = s.materialize()
        assert first == second
        fake_porcelain.clone.assert_called_once()  # not twice

    def test_reload_pulls_after_clone(self):
        fake_porcelain = MagicMock()
        with patch.dict(sys.modules, {
            "dulwich": MagicMock(porcelain=fake_porcelain),
            "dulwich.porcelain": fake_porcelain,
        }):
            s = GitSource(url="https://example.com/r.git", branch="main")
            s.materialize()
            s.reload()
        fake_porcelain.pull.assert_called_once()

    def test_reload_before_materialize_falls_back_to_clone(self):
        """Calling reload first should still work — it materializes if
        not yet cloned."""
        fake_porcelain = MagicMock()
        with patch.dict(sys.modules, {
            "dulwich": MagicMock(porcelain=fake_porcelain),
            "dulwich.porcelain": fake_porcelain,
        }):
            s = GitSource(url="https://example.com/r.git", branch="main")
            s.reload()
        fake_porcelain.clone.assert_called_once()
        fake_porcelain.pull.assert_not_called()


class TestFromSettings:
    def test_local_source(self):
        assert isinstance(from_settings(_settings()), LocalDirSource)

    def test_git_source_from_settings(self):
        s = from_settings(_settings(
            source="git",
            git_url="https://example.com/r.git",
            git_branch="develop",
            git_token="tok",
        ))
        assert isinstance(s, GitSource)
        assert s.url == "https://example.com/r.git"
        assert s.branch == "develop"
        assert s.token == "tok"

    def test_git_source_missing_url_raises(self):
        with pytest.raises(RuntimeError, match="CONFIG__GIT_URL"):
            from_settings(_settings(source="git"))

    def test_db_source_dispatches_to_dbsource(self):
        """db source returns a DbSource instance with the right
        plumbing. DbSource's own behavior is covered in
        test_config_source_db.py."""
        from core.config.source_db import DbSource
        s = from_settings(_settings(
            source="db",
            db_dsn="postgresql://localhost/postgres",
            deployment_name="inventory",
        ))
        assert isinstance(s, DbSource)
        assert s.deployment_name == "inventory"
        assert s.dsn == "postgresql://localhost/postgres"

    def test_db_source_missing_deployment_name_raises(self):
        with pytest.raises(RuntimeError, match="DEPLOYMENT_NAME"):
            from_settings(_settings(
                source="db",
                db_dsn="postgresql://localhost/postgres",
                deployment_name="",
            ))

    def test_db_source_missing_dsn_raises(self):
        with pytest.raises(RuntimeError, match="CONFIG__DB_DSN"):
            from_settings(_settings(
                source="db",
                db_dsn="",
                deployment_name="inventory",
            ))

    def test_unknown_source_raises(self):
        # The Pydantic validator rejects unknown values normally, but
        # from_settings should also fail loudly if a settings instance
        # has been constructed bypassing validation.
        s = SimpleNamespace(config=SimpleNamespace(source="bogus"))
        with pytest.raises(RuntimeError, match="Unknown CONFIG__SOURCE"):
            from_settings(s)


class TestConfigSourceBase:
    def test_base_class_methods_raise(self):
        """Plain ConfigSource isn't usable on its own — subclasses must
        implement materialize/reload."""
        s = ConfigSource()
        with pytest.raises(NotImplementedError):
            s.materialize()
        with pytest.raises(NotImplementedError):
            s.reload()


class TestGitSourceClonesRealRepo:
    """Format-validation: spin up a local file:// git repo, point
    GitSource at it, verify the clone produces the expected tree.
    Exercises real ``dulwich.porcelain`` calls — catches API drift and
    confirms our call args (branch encoding, depth, auth-URL shape)
    actually work end-to-end. Mock-based tests above can't catch that.

    Skipped when the ``[config-git]`` extra isn't installed; CI installs
    it so the test runs there.
    """

    def _seed_repo(self, repo_path: Path, files: dict) -> None:
        """Init a git repo at ``repo_path`` and commit the given files."""
        from dulwich import porcelain
        repo_path.mkdir(parents=True, exist_ok=True)
        porcelain.init(str(repo_path))
        for rel, content in files.items():
            full = repo_path / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
            porcelain.add(str(repo_path), paths=[str(full)])
        porcelain.commit(
            str(repo_path),
            message=b"seed",
            author=b"test <test@example.com>",
            committer=b"test <test@example.com>",
        )

    def test_materialize_clones_repo_and_finds_yamls(self, tmp_path):
        pytest.importorskip("dulwich")
        repo = tmp_path / "fake-config"
        self._seed_repo(repo, {
            "validation/public/products.yaml":
                "params:\n  required: [id]\n  optional: [status]\n",
            "queries/reports/by_id.yaml":
                "sql: SELECT * FROM public.t WHERE id = :id\n"
                "params:\n  required: [id]\n",
        })
        # dulwich's porcelain.init defaults to refs/heads/master.
        s = GitSource(url=f"file://{repo}", branch="master")
        materialized = s.materialize()

        assert (materialized / "validation" / "public" / "products.yaml").exists()
        assert (materialized / "queries" / "reports" / "by_id.yaml").exists()
        content = (
            materialized / "validation" / "public" / "products.yaml"
        ).read_text()
        assert "required: [id]" in content

    def test_reload_picks_up_new_commits(self, tmp_path):
        """After clone, a new commit on the source repo should land in
        the working tree after reload(). This is the path operators hit
        every time they merge a YAML change and call /admin/reload-config."""
        pytest.importorskip("dulwich")
        from dulwich import porcelain
        repo = tmp_path / "fake-config"
        self._seed_repo(repo, {
            "validation/public/products.yaml": "params:\n  required: [id]\n",
        })
        s = GitSource(url=f"file://{repo}", branch="master")
        materialized = s.materialize()
        # The newly-added file isn't there yet.
        assert not (materialized / "queries" / "added_later.yaml").exists()

        # Add + commit a new file on the source repo.
        new_file = repo / "queries" / "added_later.yaml"
        new_file.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_text("sql: SELECT 1\nparams:\n  required: []\n")
        porcelain.add(str(repo), paths=[str(new_file)])
        porcelain.commit(
            str(repo),
            message=b"add later",
            author=b"test <test@example.com>",
            committer=b"test <test@example.com>",
        )

        materialized = s.reload()
        assert (materialized / "queries" / "added_later.yaml").exists()
