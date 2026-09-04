"""Tests for setforge/cli/_git_check.py — pre-deploy git-status check.

The unit-test surface follows the same pattern as tests/test_git_ops.py:
real ``git init`` in ``tmp_path`` for the porcelain / log parsing cases
(mocks are fragile against git's argv variations + locale quirks);
``monkeypatch`` only for the non-TTY mutate-gate path and the
ls-remote-network-failure / detached-HEAD edge cases where setting up a
real remote / detached state would balloon the fixture.
"""

from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from setforge.cli._git_check import (
    GitCheckChoice,
    _format_porcelain_v2_line,
    _git_run,
    _is_detached_head,
    _render_show_diff,
    check_git_source_fresh,
    check_path_source_clean,
    prompt_git_check_choice,
    run_git_check_or_raise,
)
from setforge.errors import ConfirmRequiresInteractive
from setforge.source import GitSource, PathSource
from setforge.ui.widgets import CANCEL


def _git_init(repo: Path, *, initial_branch: str = "main") -> Path:
    """Initialize a git repo at ``repo`` with one commit on ``initial_branch``."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", initial_branch],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _create_stale_feature_cache(tmp_path: Path) -> tuple[Path, Path]:
    """Create a cache whose main is fresh while configured feature is stale."""
    publisher = _git_init(tmp_path / "publisher")
    _git("switch", "-q", "-c", "feature", cwd=publisher)
    (publisher / "README.md").write_text("# feature one\n", encoding="utf-8")
    _git("commit", "-qam", "feature one", cwd=publisher)
    _git("switch", "-q", "main", cwd=publisher)

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(publisher), str(origin)], check=True
    )
    cache = tmp_path / "cache"
    subprocess.run(["git", "clone", "-q", str(origin), str(cache)], check=True)
    _git("switch", "-q", "feature", cwd=cache)

    _git("remote", "add", "origin", str(origin), cwd=publisher)
    _git("switch", "-q", "feature", cwd=publisher)
    (publisher / "README.md").write_text("# feature two\n", encoding="utf-8")
    _git("commit", "-qam", "feature two", cwd=publisher)
    _git("push", "-q", "origin", "feature", cwd=publisher)
    return origin, cache


# ---------------------------------------------------------------------------
# _git_run — locale lockdown
# ---------------------------------------------------------------------------


class TestGitRun:
    def test_sets_locale_c_in_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_git_run`` must inject LANG=C and LC_ALL=C on every invocation."""
        captured_env: dict[str, str] = {}

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured_env.update(kwargs.get("env", {}))
            return subprocess.CompletedProcess(args[0], 0, "", "")

        monkeypatch.setattr("setforge.cli._git_check.subprocess.run", fake_run)
        _git_run(["git", "status"], cwd=tmp_path)
        assert captured_env.get("LANG") == "C"
        assert captured_env.get("LC_ALL") == "C"


# ---------------------------------------------------------------------------
# check_path_source_clean
# ---------------------------------------------------------------------------


class TestCheckPathSourceClean:
    def test_returns_empty_for_clean_repo(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        assert check_path_source_clean(repo) == []

    def test_parses_modified_added_untracked(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        # Modified tracked file
        (repo / "README.md").write_text("# changed\n")
        # Staged new file
        (repo / "added.txt").write_text("added\n")
        subprocess.run(["git", "add", "added.txt"], cwd=repo, check=True)
        # Untracked file
        (repo / "new.txt").write_text("new\n")
        lines = check_path_source_clean(repo)
        # Three dirty entries (modified, staged-add, untracked).
        assert len(lines) == 3
        joined = "\n".join(lines)
        assert "README.md" in joined
        assert "added.txt" in joined
        assert "new.txt" in joined
        assert "??" in joined  # untracked marker

    def test_returns_empty_for_non_git_directory(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        assert check_path_source_clean(plain) == []

    def test_bare_repo_proceeds_with_empty_result(self, tmp_path: Path) -> None:
        """Bare repos have no working tree to dirty — treat as clean."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        subprocess.run(
            ["git", "init", "-q", "--bare"],
            cwd=bare,
            check=True,
            capture_output=True,
        )
        assert check_path_source_clean(bare) == []

    def test_ignores_submodule_dirt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--ignore-submodules=all`` keeps nested untracked files invisible."""
        # Submodule fixture is complex; assert via stub: confirm the
        # subprocess call carries the flag rather than constructing a
        # real submodule.
        repo = _git_init(tmp_path / "repo")
        captured_cmd: list[str] = []

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0]
            if "--is-inside-work-tree" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "true\n", "")
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("setforge.cli._git_check.subprocess.run", fake_run)
        check_path_source_clean(repo)
        assert "--ignore-submodules=all" in captured_cmd


# ---------------------------------------------------------------------------
# _format_porcelain_v2_line
# ---------------------------------------------------------------------------


class TestFormatPorcelainV2Line:
    def test_ordinary_modified_entry(self) -> None:
        line = "1 .M N... 100644 100644 100644 abc def README.md"
        assert _format_porcelain_v2_line(line) == ".M README.md"

    def test_untracked_entry(self) -> None:
        assert _format_porcelain_v2_line("? new.txt") == "?? new.txt"

    def test_ignored_entry(self) -> None:
        assert _format_porcelain_v2_line("! ignored.txt") == "!! ignored.txt"

    def test_unknown_shape_returns_raw(self) -> None:
        line = "weird line shape"
        # Untouched (not '?', '!', '1', '2', 'u' as the first token's tag).
        assert _format_porcelain_v2_line(line) == line


# ---------------------------------------------------------------------------
# _is_detached_head
# ---------------------------------------------------------------------------


class TestIsDetachedHead:
    def test_parses_branch_head_detached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``# branch.head (detached)`` porcelain-v2 line → True."""
        repo = _git_init(tmp_path / "repo")
        stdout = (
            "# branch.oid 0123456789abcdef0123456789abcdef01234567\n"
            "# branch.head (detached)\n"
        )

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args[0], 0, stdout, "")

        monkeypatch.setattr("setforge.cli._git_check.subprocess.run", fake_run)
        assert _is_detached_head(repo) is True

    def test_parses_branch_head_named_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``# branch.head main`` → False (HEAD is on a branch)."""
        repo = _git_init(tmp_path / "repo")
        stdout = (
            "# branch.oid 0123456789abcdef0123456789abcdef01234567\n"
            "# branch.head main\n"
        )

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args[0], 0, stdout, "")

        monkeypatch.setattr("setforge.cli._git_check.subprocess.run", fake_run)
        assert _is_detached_head(repo) is False


# ---------------------------------------------------------------------------
# check_git_source_fresh
# ---------------------------------------------------------------------------


class TestCheckGitSourceFresh:
    def test_returns_empty_for_non_git_directory(self, tmp_path: Path) -> None:
        plain = tmp_path / "cache"
        plain.mkdir()
        assert check_git_source_fresh(plain) == ()

    def test_returns_empty_when_up_to_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local SHA == remote SHA → cache is fresh."""
        cache = _git_init(tmp_path / "cache")
        # Wire a stub origin pointing at the same commit.
        local_sha = subprocess.run(
            ["git", "-C", str(cache), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0]
            if "--is-inside-work-tree" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "true\n", "")
            if cmd[3:5] == ["symbolic-ref", "--short"]:
                return subprocess.CompletedProcess(cmd, 0, "origin/main\n", "")
            if cmd[3] == "ls-remote":
                stdout = f"{local_sha}\trefs/heads/main\n"
                return subprocess.CompletedProcess(cmd, 0, stdout, "")
            if cmd[3] == "rev-parse":
                return subprocess.CompletedProcess(cmd, 0, f"{local_sha}\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("setforge.cli._git_check.subprocess.run", fake_run)
        assert check_git_source_fresh(cache) == ()

    def test_returns_commits_behind_remote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local lags by 2 commits → tuple has 2 oneline entries."""
        cache = _git_init(tmp_path / "cache")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0]
            if "--is-inside-work-tree" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "true\n", "")
            if cmd[3:5] == ["symbolic-ref", "--short"]:
                return subprocess.CompletedProcess(cmd, 0, "origin/main\n", "")
            if cmd[3] == "ls-remote":
                stdout = "deadbeef\trefs/heads/main\n"
                return subprocess.CompletedProcess(cmd, 0, stdout, "")
            if cmd[3] == "rev-parse":
                return subprocess.CompletedProcess(cmd, 0, "cafef00d\n", "")
            if cmd[3] == "log":
                return subprocess.CompletedProcess(
                    cmd, 0, "abc1234 second commit\ndef5678 third commit\n", ""
                )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("setforge.cli._git_check.subprocess.run", fake_run)
        result = check_git_source_fresh(cache)
        assert result == ("abc1234 second commit", "def5678 third commit")

    def test_network_failure_warns_and_returns_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ls-remote non-zero exit → warn-and-proceed (empty tuple)."""
        cache = _git_init(tmp_path / "cache")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0]
            if "--is-inside-work-tree" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "true\n", "")
            if cmd[3:5] == ["symbolic-ref", "--short"]:
                return subprocess.CompletedProcess(cmd, 0, "origin/main\n", "")
            if cmd[3] == "ls-remote":
                return subprocess.CompletedProcess(
                    cmd, 128, "", "fatal: unable to access remote"
                )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("setforge.cli._git_check.subprocess.run", fake_run)
        assert check_git_source_fresh(cache) == ()
        captured = capsys.readouterr()
        assert "ls-remote failed" in captured.err

    def test_ls_remote_timeout_warns_and_returns_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``TimeoutExpired`` on ls-remote → warn-and-proceed."""
        cache = _git_init(tmp_path / "cache")

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0]
            if "--is-inside-work-tree" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "true\n", "")
            if cmd[3:5] == ["symbolic-ref", "--short"]:
                return subprocess.CompletedProcess(cmd, 0, "origin/main\n", "")
            if cmd[3] == "ls-remote":
                raise subprocess.TimeoutExpired(cmd, 30)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("setforge.cli._git_check.subprocess.run", fake_run)
        assert check_git_source_fresh(cache) == ()
        captured = capsys.readouterr()
        assert "timed out" in captured.err


# ---------------------------------------------------------------------------
# prompt_git_check_choice — mutate-gate
# ---------------------------------------------------------------------------


class TestPromptGitCheckChoice:
    def test_non_tty_raises_confirm_requires_interactive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Non-TTY caller without ``--no-git-check`` must RAISE, not fall back."""
        monkeypatch.setattr("setforge.cli._git_check.sys.stdin.isatty", lambda: False)
        source = PathSource(path=tmp_path)
        with pytest.raises(ConfirmRequiresInteractive):
            prompt_git_check_choice(
                source=source, dirty_lines=[" M file"], detached=False
            )

    def test_tty_dispatches_to_button_bar(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("setforge.cli._git_check.sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "setforge.cli._git_check.button_bar",
            lambda *a, **k: GitCheckChoice.PROCEED,
        )
        source = PathSource(path=tmp_path)
        assert (
            prompt_git_check_choice(
                source=source, dirty_lines=[" M file"], detached=False
            )
            is GitCheckChoice.PROCEED
        )

    def test_dialog_returns_cancel_treated_as_abort(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("setforge.cli._git_check.sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "setforge.cli._git_check.button_bar",
            lambda *a, **k: CANCEL,
        )
        source = PathSource(path=tmp_path)
        assert (
            prompt_git_check_choice(
                source=source, dirty_lines=[" M file"], detached=False
            )
            is GitCheckChoice.ABORT
        )


# ---------------------------------------------------------------------------
# run_git_check_or_raise — end-to-end flow
# ---------------------------------------------------------------------------


class TestRunGitCheckOrRaise:
    def test_no_git_check_short_circuits(self, tmp_path: Path) -> None:
        """``--no-git-check`` bypasses every check — no exceptions, no prompts."""
        source = PathSource(path=tmp_path)
        # No git repo at tmp_path, but flag is set → must return None.
        run_git_check_or_raise(source=source, no_git_check=True)

    def test_clean_path_source_returns_silently(self, tmp_path: Path) -> None:
        repo = _git_init(tmp_path / "repo")
        source = PathSource(path=repo)
        run_git_check_or_raise(source=source, no_git_check=False)

    def test_dirty_path_source_non_tty_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dirty path source + non-TTY caller → mutate-gate raises."""
        repo = _git_init(tmp_path / "repo")
        (repo / "README.md").write_text("# dirty\n")
        monkeypatch.setattr("setforge.cli._git_check.sys.stdin.isatty", lambda: False)
        source = PathSource(path=repo)
        with pytest.raises(ConfirmRequiresInteractive):
            run_git_check_or_raise(source=source, no_git_check=False)

    def test_dirty_path_source_proceed_choice_returns_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User picks PROCEED → function returns None (install continues)."""
        repo = _git_init(tmp_path / "repo")
        (repo / "README.md").write_text("# dirty\n")
        monkeypatch.setattr("setforge.cli._git_check.sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "setforge.cli._git_check.button_bar",
            lambda *a, **k: GitCheckChoice.PROCEED,
        )
        source = PathSource(path=repo)
        run_git_check_or_raise(source=source, no_git_check=False)

    def test_dirty_path_source_abort_choice_raises_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User picks ABORT → typer.Exit(1)."""
        import typer

        repo = _git_init(tmp_path / "repo")
        (repo / "README.md").write_text("# dirty\n")
        monkeypatch.setattr("setforge.cli._git_check.sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "setforge.cli._git_check.button_bar",
            lambda *a, **k: GitCheckChoice.ABORT,
        )
        source = PathSource(path=repo)
        with pytest.raises(typer.Exit) as exc:
            run_git_check_or_raise(source=source, no_git_check=False)
        assert exc.value.exit_code == 1

    def test_show_diff_then_proceed_loops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SHOW_DIFF is an inspection step — re-prompt until ABORT or PROCEED."""
        repo = _git_init(tmp_path / "repo")
        (repo / "README.md").write_text("# dirty\n")
        monkeypatch.setattr("setforge.cli._git_check.sys.stdin.isatty", lambda: True)
        choices = iter([GitCheckChoice.SHOW_DIFF, GitCheckChoice.PROCEED])
        monkeypatch.setattr(
            "setforge.cli._git_check.button_bar",
            lambda *a, **k: next(choices),
        )
        source = PathSource(path=repo)
        run_git_check_or_raise(source=source, no_git_check=False)


# ---------------------------------------------------------------------------
# Smoke: GitSource path through run_git_check_or_raise (no real network)
# ---------------------------------------------------------------------------


class TestGitSourceCachePath:
    def test_git_source_checks_configured_ref_not_remote_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        origin, cache = _create_stale_feature_cache(tmp_path)
        source = GitSource(url=str(origin), ref="feature", clone_dest=cache)
        monkeypatch.setattr("setforge.cli._git_check.sys.stdin.isatty", lambda: False)

        with pytest.raises(ConfirmRequiresInteractive, match="stale state"):
            run_git_check_or_raise(source=source, no_git_check=False)

    def test_show_diff_uses_configured_ref(self, tmp_path: Path) -> None:
        origin, cache = _create_stale_feature_cache(tmp_path)
        _git("fetch", "-q", "origin", "feature", cwd=cache)
        source = GitSource(url=str(origin), ref="feature", clone_dest=cache)
        output = StringIO()

        _render_show_diff(
            source,
            console=Console(file=output, color_system=None, width=120),
        )

        assert "feature two" in output.getvalue()

    def test_unclonable_git_source_cache_skips_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-existent clone_dest → resolve_source_dir raises SourceNotCloned.

        We surface the canonical error path: ``run_git_check_or_raise``
        should propagate :class:`SourceNotCloned` rather than swallow
        it, because install cannot proceed without a clone anyway.
        """
        from setforge.errors import SourceNotCloned

        source = GitSource(
            url="https://example.invalid/x.git",
            clone_dest=tmp_path / "missing-clone",
        )
        with pytest.raises(SourceNotCloned):
            run_git_check_or_raise(source=source, no_git_check=False)

    def test_git_source_cache_existing_clone_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing cache, ls-remote SHA matches local HEAD → no prompt fires."""
        cache = _git_init(tmp_path / "cache")
        local_sha = subprocess.run(
            ["git", "-C", str(cache), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cmd = args[0]
            if "--is-inside-work-tree" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "true\n", "")
            if cmd[3:5] == ["symbolic-ref", "--short"]:
                return subprocess.CompletedProcess(cmd, 0, "origin/main\n", "")
            if cmd[3] == "ls-remote":
                stdout = f"{local_sha}\trefs/heads/main\n"
                return subprocess.CompletedProcess(cmd, 0, stdout, "")
            if cmd[3] == "rev-parse":
                return subprocess.CompletedProcess(cmd, 0, f"{local_sha}\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("setforge.cli._git_check.subprocess.run", fake_run)
        source = GitSource(url="https://example.test/x.git", clone_dest=cache)
        # No prompt — silent return.
        run_git_check_or_raise(source=source, no_git_check=False)
