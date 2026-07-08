"""Tests for the A0 fetch-upstream wiring in ``setforge install``.

``install`` fetches the git config source (via :func:`source.fetch_source`)
BEFORE the pre-deploy git-check, so the pulled content is what gets
reconciled. ``--no-fetch`` skips the pull entirely for offline / CI runs.
A :class:`PathSource` (the legacy ``--config`` shape these tests use) is a
no-op inside ``fetch_source``, so the wiring is asserted via a spy on the
call rather than real git ops.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import git_ops
from setforge import source as source_mod
from setforge.cli import app
from setforge.errors import SourceNotCloned
from setforge.source import GitSource, PathSource

_PROFILE = "test-fetch"


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_fetch/doc.md\n"
        "    disposition: shared\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n",
        encoding="utf-8",
    )
    (repo / "tracked").mkdir(parents=True, exist_ok=True)
    (repo / "tracked" / "doc.md").write_text("# doc\n", encoding="utf-8")
    return config


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _install(config: Path, *extra: str) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
        *extra,
    ]
    return CliRunner().invoke(app, args)


class TestFetchWiring:
    def test_install_fetches_by_default(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []

        def _spy(src: object) -> str:
            calls.append(src)
            return "ok"

        monkeypatch.setattr(source_mod, "fetch_source", _spy)
        config = _write_config(repo)
        result = _install(config)
        assert result.exit_code == 0, result.output
        assert len(calls) == 1

    def test_no_fetch_skips_the_pull(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []

        def _spy(src: object) -> str:
            calls.append(src)
            return "ok"

        monkeypatch.setattr(source_mod, "fetch_source", _spy)
        config = _write_config(repo)
        result = _install(config, "--no-fetch")
        assert result.exit_code == 0, result.output
        assert calls == []

    def test_git_source_fetch_message_echoed(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the source is a real GitSource, the fetch status line surfaces.
        git_src = GitSource(url="https://example.invalid/cfg.git", ref="main")
        monkeypatch.setattr(
            "setforge.cli.install.resolve_source_for_git_check",
            lambda _repo_root: git_src,
        )
        monkeypatch.setattr(
            source_mod, "fetch_source", lambda _src: "fetched and checked out main"
        )
        # git-check also resolves a source; keep it from touching the network.
        monkeypatch.setattr(
            "setforge.cli.install.run_git_check_or_raise",
            lambda **_kw: None,
        )
        config = _write_config(repo)
        result = _install(config)
        assert result.exit_code == 0, result.output
        assert "fetched and checked out main" in result.output

    def test_path_source_fetch_is_silent(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A PathSource fetch no-ops; its status line must NOT clutter output.
        monkeypatch.setattr(
            "setforge.cli.install.resolve_source_for_git_check",
            lambda repo_root: PathSource(path=repo_root),
        )
        config = _write_config(repo)
        result = _install(config)
        assert result.exit_code == 0, result.output
        assert "nothing to fetch" not in result.output

    def test_no_fetch_runs_no_git_operation(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Spies _run_git (the single shell-out seam) rather than global
        # subprocess.run, which would false-positive on the transition's own
        # `git rev-parse HEAD` provenance call.
        calls: list[object] = []

        def _spy(*args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            raise AssertionError("no git operation must run under --no-fetch")

        monkeypatch.setattr(git_ops, "_run_git", _spy)
        config = _write_config(repo)
        result = _install(config, "--no-fetch")
        assert result.exit_code == 0, result.output
        assert calls == []

    def test_no_fetch_missing_git_clone_raises_source_not_cloned(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --no-git-check is NOT passed, so the check runs and surfaces the
        # missing clone as SourceNotCloned (no silent network touch).
        git_src = GitSource(url="https://example.invalid/cfg.git", ref="main")
        monkeypatch.setattr(
            "setforge.cli.install.resolve_source_for_git_check",
            lambda _repo_root: git_src,
        )
        config = _write_config(repo)
        result = CliRunner().invoke(
            app,
            [
                "install",
                f"--profile={_PROFILE}",
                f"--config={config}",
                "--no-secrets-scan",
                "--yes",
                "--no-fetch",
            ],
        )
        assert result.exit_code != 0
        assert isinstance(result.exception, SourceNotCloned)
