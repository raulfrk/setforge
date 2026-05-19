"""End-to-end tests for ``setforge status`` (mockup O / setforge-xra8).

Drives the real CLI via Typer's :class:`CliRunner` against synthetic
config repos and tmp ``SETFORGE_STATE_DIR``. Read-only command: every
test asserts ``result.exit_code == 0`` unless the case is exercising a
hard-error path (no source configured, unknown profile, etc.).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from setforge.cli import app
from setforge.cli import status as status_mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_minimal_config(tmp_path: Path, *, profile: str = "vm-headless") -> Path:
    """Build a minimal setforge.yaml under ``tmp_path``; return its path."""
    tracked = tmp_path / "tracked" / "doc.md"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("hello\n", encoding="utf-8")
    yaml_path = tmp_path / "setforge.yaml"
    yaml_path.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.local/share/setforge-test/doc.md\n"
        "profiles:\n"
        f"  {profile}:\n"
        "    tracked_files: [doc]\n",
        encoding="utf-8",
    )
    return yaml_path


def _invoke_status(
    *,
    source_dir: Path,
    config_path: Path,
    profile: str = "vm-headless",
) -> Result:
    """Invoke ``setforge status`` with explicit ``--source`` + ``--config``."""
    return CliRunner().invoke(
        app,
        [
            "--source",
            str(source_dir),
            "status",
            "--config",
            str(config_path),
            "--profile",
            profile,
        ],
    )


def _stub_transition(
    state_root: Path,
    *,
    profile: str,
    dirname: str,
    timestamp: str = "2026-05-18T07:00:15+00:00",
    source_sha: str | None = None,
    command: str = "install",
) -> Path:
    """Materialize one transition meta.json under ``state_root/transitions``."""
    root = state_root / "transitions"
    root.mkdir(parents=True, exist_ok=True)
    target = root / dirname
    target.mkdir()
    meta: dict[str, str] = {
        "command": command,
        "profile": profile,
        "timestamp": timestamp,
        "host": "h",
        "version": "0.2.0",
    }
    if source_sha is not None:
        meta["source_sha"] = source_sha
    (target / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Pure-helper tests (no subprocess / no CliRunner)
# ---------------------------------------------------------------------------


def test_format_age_seconds_to_days() -> None:
    """``_format_age`` must pick the largest unit that fits, never zero-up."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    assert status_mod._format_age(now, now - timedelta(seconds=5)) == "5s ago"
    assert status_mod._format_age(now, now - timedelta(minutes=3)) == "3m ago"
    assert status_mod._format_age(now, now - timedelta(hours=12)) == "12h ago"
    assert status_mod._format_age(now, now - timedelta(days=2)) == "2d ago"


def test_format_age_clamps_negative_delta() -> None:
    """A clock-skew ``then`` in the future must not surface a negative age."""
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    assert status_mod._format_age(now, now + timedelta(seconds=10)) == "0s ago"


def test_read_overlay_counts_missing_file_returns_empty(tmp_path: Path) -> None:
    """Absent local.yaml → empty overlay map (no exception)."""
    assert status_mod._read_overlay_counts(tmp_path / "nope.yaml") == {}


def test_read_overlay_counts_parses_lists_and_mappings(tmp_path: Path) -> None:
    """List blocks count by length; mapping blocks count by key count."""
    local = tmp_path / "local.yaml"
    local.write_text(
        "extensions:\n  include:\n    - foo\n    - bar\n"
        "marketplaces:\n  work-internal: github:co/internal\n"
        "host_local_sections:\n  doc:\n    - per-host\n",
        encoding="utf-8",
    )
    counts = status_mod._read_overlay_counts(local)
    # `extensions` here is a mapping → count = 1 (the `include` key).
    assert counts["extensions"] == 1
    assert counts["marketplaces"] == 1
    assert counts["host_local_sections"] == 1


def test_read_overlay_counts_ignores_scalar_blocks(tmp_path: Path) -> None:
    """Non-list / non-mapping values (e.g. a stray scalar) must be skipped."""
    local = tmp_path / "local.yaml"
    local.write_text("extensions: not-a-mapping-or-list\n", encoding="utf-8")
    assert status_mod._read_overlay_counts(local) == {}


def test_read_overlay_counts_handles_malformed_yaml(tmp_path: Path) -> None:
    """A malformed YAML must surface as ``{}`` (status never blocks)."""
    local = tmp_path / "local.yaml"
    local.write_text("extensions: [unterminated\n", encoding="utf-8")
    assert status_mod._read_overlay_counts(local) == {}


# ---------------------------------------------------------------------------
# Git-info resolution tests (monkeypatched subprocess)
# ---------------------------------------------------------------------------


class _FakeGitRunner:
    """Stand-in for :func:`subprocess.run` that maps args to canned results.

    Tests register expected arg suffixes via :meth:`add` and read back the
    full call log in :attr:`calls` for invocation-count assertions.
    """

    def __init__(self) -> None:
        self._cases: list[
            tuple[tuple[str, ...], int, str]
        ] = []  # (suffix, returncode, stdout)
        self.calls: list[list[str]] = []

    def add(self, suffix: tuple[str, ...], returncode: int, stdout: str) -> None:
        self._cases.append((suffix, returncode, stdout))

    def __call__(
        self, args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        for suffix, returncode, stdout in self._cases:
            if tuple(args)[-len(suffix) :] == suffix:
                return subprocess.CompletedProcess(
                    args=args, returncode=returncode, stdout=stdout, stderr=""
                )
        return subprocess.CompletedProcess(
            args=args, returncode=128, stdout="", stderr="unmocked"
        )


def test_resolve_git_info_not_a_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the source dir is not a git repo, both git fields fall back."""
    runner = _FakeGitRunner()
    # First call: --is-inside-work-tree → fails ("not a repo")
    runner.add(("--is-inside-work-tree",), returncode=128, stdout="")
    monkeypatch.setattr(status_mod.subprocess, "run", runner)
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/bin/git")

    info = status_mod._resolve_git_info(tmp_path, prev_sha=None)
    assert info.head_short is None
    assert info.commits_since_install is None
    assert info.commits_since_install_reason == "config dir not a git repo"
    assert info.commits_vs_origin is None
    assert info.commits_vs_origin_reason == "config dir not a git repo"


def test_resolve_git_info_no_origin_main_shows_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When ``origin/main`` is not configured, status shows a placeholder."""
    runner = _FakeGitRunner()
    runner.add(("--is-inside-work-tree",), returncode=0, stdout="true\n")
    runner.add(("HEAD",), returncode=0, stdout="1f37cb1\n")
    runner.add(("origin/main",), returncode=128, stdout="")  # rev-parse --verify
    monkeypatch.setattr(status_mod.subprocess, "run", runner)
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/bin/git")

    info = status_mod._resolve_git_info(tmp_path, prev_sha=None)
    assert info.head_short == "1f37cb1"
    assert info.commits_vs_origin is None
    assert info.commits_vs_origin_reason == "no origin/main remote"


def test_resolve_git_info_prev_sha_none_surfaces_schema_bump_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A None prev_sha must surface the schema-bump placeholder verbatim."""
    runner = _FakeGitRunner()
    runner.add(("--is-inside-work-tree",), returncode=0, stdout="true\n")
    runner.add(("HEAD",), returncode=0, stdout="1f37cb1\n")
    runner.add(("origin/main",), returncode=0, stdout="abc\n")
    runner.add(("origin/main..HEAD",), returncode=0, stdout="4\n")
    monkeypatch.setattr(status_mod.subprocess, "run", runner)
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/bin/git")

    info = status_mod._resolve_git_info(tmp_path, prev_sha=None)
    assert info.commits_since_install is None
    assert info.commits_since_install_reason == (
        "requires source_sha; this transition predates schema bump"
    )
    assert info.commits_vs_origin == 4


def test_resolve_git_info_records_counts_when_prev_sha_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Happy path: prev_sha and origin/main both available."""
    runner = _FakeGitRunner()
    runner.add(("--is-inside-work-tree",), returncode=0, stdout="true\n")
    runner.add(("HEAD",), returncode=0, stdout="1f37cb1\n")
    runner.add(("deadbeef..HEAD",), returncode=0, stdout="2\n")
    runner.add(("origin/main",), returncode=0, stdout="abc\n")
    runner.add(("origin/main..HEAD",), returncode=0, stdout="0\n")
    monkeypatch.setattr(status_mod.subprocess, "run", runner)
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/bin/git")

    info = status_mod._resolve_git_info(tmp_path, prev_sha="deadbeef")
    assert info.commits_since_install == 2
    assert info.commits_since_install_reason is None
    assert info.commits_vs_origin == 0
    assert info.commits_vs_origin_reason is None


def test_git_run_returns_127_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing ``git`` on PATH must not raise; returncode 127 is the signal."""
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: None)
    result = status_mod._git_run(["status"], cwd=tmp_path)
    assert result.returncode == 127
    assert "not on PATH" in result.stderr


# ---------------------------------------------------------------------------
# CliRunner end-to-end tests (full status command, mocking subprocess + probe)
# ---------------------------------------------------------------------------


def _patch_git_for_clean_repo(monkeypatch: pytest.MonkeyPatch) -> _FakeGitRunner:
    """Install a happy-path git runner that resolves every status query."""
    runner = _FakeGitRunner()
    runner.add(("--is-inside-work-tree",), returncode=0, stdout="true\n")
    runner.add(("HEAD",), returncode=0, stdout="1f37cb1\n")
    runner.add(("origin/main",), returncode=0, stdout="abc\n")
    runner.add(("origin/main..HEAD",), returncode=0, stdout="0\n")
    monkeypatch.setattr(status_mod.subprocess, "run", runner)
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/bin/git")
    return runner


def test_status_renders_5_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The five mockup sections must all appear in the output."""
    config_path = _write_minimal_config(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    _stub_transition(
        state_dir,
        profile="vm-headless",
        dirname="20260518T070015000000Z-install-vm-headless",
        source_sha="deadbeef",
    )
    _patch_git_for_clean_repo(monkeypatch)

    # When source_sha is present, status will also try `<sha>..HEAD`.
    # Inject the matching case onto the same patched runner.
    captured_runner = status_mod.subprocess.run
    assert isinstance(captured_runner, _FakeGitRunner)
    captured_runner.add(("deadbeef..HEAD",), returncode=0, stdout="0\n")

    result = _invoke_status(source_dir=tmp_path, config_path=config_path)

    assert result.exit_code == 0, result.output
    assert "=== setforge status — vm-headless" in result.output
    assert "config-repo:" in result.output
    assert "1f37cb1" in result.output
    assert "last install:" in result.output
    assert "drift:" in result.output
    assert "overlay:" in result.output
    assert "capabilities:" in result.output


def test_status_exit_0_when_capabilities_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status is informational — missing claude/code binaries do not gate."""
    config_path = _write_minimal_config(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    _patch_git_for_clean_repo(monkeypatch)
    # Force all binaries missing — capabilities should render disabled rows
    # but the command still exits 0.
    monkeypatch.setattr("setforge.cli._init_helpers.resolve_binary", lambda name: None)
    monkeypatch.setattr("setforge.cli._init_helpers._resolve_uv", lambda: None)

    result = _invoke_status(source_dir=tmp_path, config_path=config_path)

    assert result.exit_code == 0, result.output
    # The "disabled" capability mark renders as ✗ — at least one row should
    # show it given resolve_binary stub above (claude_plugins / vscode).
    assert "✗" in result.output


def test_status_old_transition_no_source_sha_shows_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old transition (no source_sha key) must surface the schema-bump note."""
    config_path = _write_minimal_config(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    _stub_transition(
        state_dir,
        profile="vm-headless",
        dirname="20260518T070015000000Z-install-vm-headless",
        source_sha=None,  # explicit: pre-bump transition
    )
    _patch_git_for_clean_repo(monkeypatch)

    result = _invoke_status(source_dir=tmp_path, config_path=config_path)

    assert result.exit_code == 0, result.output
    assert "requires source_sha" in result.output


def test_status_no_origin_main_shows_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When origin/main is not configured, the vs-origin line shows a placeholder."""
    config_path = _write_minimal_config(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    runner = _FakeGitRunner()
    runner.add(("--is-inside-work-tree",), returncode=0, stdout="true\n")
    runner.add(("HEAD",), returncode=0, stdout="1f37cb1\n")
    runner.add(("origin/main",), returncode=128, stdout="")
    monkeypatch.setattr(status_mod.subprocess, "run", runner)
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/bin/git")

    result = _invoke_status(source_dir=tmp_path, config_path=config_path)

    assert result.exit_code == 0, result.output
    assert "no origin/main remote" in result.output


def test_status_config_dir_not_git_repo_shows_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-git source dir must surface the ``config dir not a git repo`` line."""
    config_path = _write_minimal_config(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    runner = _FakeGitRunner()
    runner.add(("--is-inside-work-tree",), returncode=128, stdout="")
    monkeypatch.setattr(status_mod.subprocess, "run", runner)
    monkeypatch.setattr(status_mod.shutil, "which", lambda name: "/usr/bin/git")

    result = _invoke_status(source_dir=tmp_path, config_path=config_path)

    assert result.exit_code == 0, result.output
    assert "config dir not a git repo" in result.output


def test_status_no_transitions_recorded_shows_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile with no transition history must say so in the last-install line."""
    config_path = _write_minimal_config(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    _patch_git_for_clean_repo(monkeypatch)

    result = _invoke_status(source_dir=tmp_path, config_path=config_path)

    assert result.exit_code == 0, result.output
    assert "no transitions recorded" in result.output


def test_status_skips_later_sync_for_last_install_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later SYNC must NOT be rendered under the ``last install:`` label.

    Regression: ``_load_last_install_meta`` previously called
    ``load_latest(profile)`` unconditionally, which returns the latest
    transition of ANY command type. When a sync lands after an install,
    the sync's transition (no source_sha) was rendered with the
    misleading "requires source_sha; this transition predates schema
    bump" placeholder. The fix filters to INSTALL only.
    """
    config_path = _write_minimal_config(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    # Older install with source_sha set.
    _stub_transition(
        state_dir,
        profile="vm-headless",
        dirname="20260518T070015000000Z-install-vm-headless",
        source_sha="deadbeef",
        command="install",
    )
    # Newer sync — no source_sha; under the old code this would shadow
    # the install in the "last install:" line.
    _stub_transition(
        state_dir,
        profile="vm-headless",
        dirname="20260518T080015000000Z-sync-vm-headless",
        timestamp="2026-05-18T08:00:15+00:00",
        command="sync",
    )
    runner = _patch_git_for_clean_repo(monkeypatch)
    # source_sha = deadbeef triggers an extra `deadbeef..HEAD` git query.
    runner.add(("deadbeef..HEAD",), returncode=0, stdout="0\n")

    result = _invoke_status(source_dir=tmp_path, config_path=config_path)

    assert result.exit_code == 0, result.output
    # The install dirname renders in the last-install line; the sync does not.
    assert "20260518T070015000000Z-install-vm-headless" in result.output
    assert "20260518T080015000000Z-sync-vm-headless" not in result.output
    # And the misleading schema-bump placeholder must NOT appear (the
    # install carries a source_sha, so commits-since-install is concrete).
    assert "requires source_sha" not in result.output


def test_status_unknown_profile_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown profile must surface a clear non-zero exit via SetforgeError."""
    config_path = _write_minimal_config(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))

    result = _invoke_status(
        source_dir=tmp_path,
        config_path=config_path,
        profile="does-not-exist",
    )

    assert result.exit_code != 0
