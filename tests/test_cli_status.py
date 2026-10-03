"""End-to-end tests for ``setforge status`` (mockup O).

Drives the real CLI via Typer's :class:`CliRunner` against synthetic
config repos and tmp ``SETFORGE_STATE_DIR``. Read-only command: every
test asserts ``result.exit_code == 0`` unless the case is exercising a
hard-error path (no source configured, unknown profile, etc.).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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


def _write_codex_instruction_config(tmp_path: Path) -> Path:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "codex:\n"
        "  instructions:\n"
        "    base: {source: codex/AGENTS.md}\n"
        "profiles:\n"
        "  vm-headless:\n"
        "    codex:\n"
        "      instructions: [base]\n",
        encoding="utf-8",
    )
    return config


def _write_empty_config(tmp_path: Path) -> Path:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "profiles:\n"
        "  vm-headless: {}\n",
        encoding="utf-8",
    )
    return config


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


@pytest.mark.parametrize("output_format", ["human", "json"])
@pytest.mark.parametrize("other_source", ["absent", "environment", "flag"])
def test_status_explicit_config_owns_repository_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
    other_source: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    local_config = home / ".config" / "setforge" / "local.yaml"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    monkeypatch.delenv("SETFORGE_SOURCE", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local_config)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local_config)
    monkeypatch.setattr(status_mod, "LOCAL_CONFIG_PATH", local_config)
    monkeypatch.setattr(
        status_mod, "probe_environment", lambda **_kw: SimpleNamespace(capabilities=())
    )
    selected = tmp_path / "selected"
    selected.mkdir()
    config = _write_empty_config(selected)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_empty_config(elsewhere)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    from setforge import transitions
    from setforge.ownership import OwnershipStore

    for runtime_path in (Path.home(), transitions.state_root(), OwnershipStore().root):
        assert runtime_path.resolve().is_relative_to(tmp_path.resolve())

    def git(*args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(selected),
                "-c",
                "commit.gpgsign=false",
                "-c",
                f"core.hooksPath={tmp_path / 'hooks'}",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("add", "setforge.yaml")
    git("commit", "-qm", "fixture")
    expected_head = git("rev-parse", "--short", "HEAD")
    args = [f"--format={output_format}"]
    if other_source == "environment":
        monkeypatch.setenv("SETFORGE_SOURCE", str(elsewhere))
    elif other_source == "flag":
        args.extend(["--source", str(elsewhere)])

    result = CliRunner().invoke(
        app, [*args, "status", "--config", str(config), "--profile", "vm-headless"]
    )

    assert result.exit_code == 0, result.output + str(result.exception)
    if output_format == "json":
        report = json.loads(result.stdout)["data"]["config_repo"]
        assert report["source_dir"] == str(selected.resolve())
        assert report["head_short"] == expected_head
    else:
        assert (
            f"config-repo:    {selected.resolve()} @ {expected_head}" in result.stdout
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
    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    assert status_mod._format_age(now, now - timedelta(seconds=5)) == "5s ago"
    assert status_mod._format_age(now, now - timedelta(minutes=3)) == "3m ago"
    assert status_mod._format_age(now, now - timedelta(hours=12)) == "12h ago"
    assert status_mod._format_age(now, now - timedelta(days=2)) == "2d ago"


def test_format_age_clamps_negative_delta() -> None:
    """A clock-skew ``then`` in the future must not surface a negative age."""
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
        "tracked_files:\n  doc:\n    src: doc.md\n",
        encoding="utf-8",
    )
    counts = status_mod._read_overlay_counts(local)
    # `extensions` here is a mapping → count = 1 (the `include` key).
    assert counts["extensions"] == 1
    assert counts["marketplaces"] == 1
    assert counts["tracked_files"] == 1


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


@pytest.mark.parametrize(("ahead", "behind"), [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_status_reports_native_git_ahead_and_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ahead: int, behind: int
) -> None:
    config = _write_empty_config(tmp_path)

    def git(*args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=SetForge test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    git("init")
    git("add", "setforge.yaml")
    git("commit", "-m", "common")
    common = git("rev-parse", "HEAD")
    for index in range(behind):
        git("commit", "--allow-empty", "-m", f"remote {index}")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("reset", "--hard", common)
    for index in range(ahead):
        git("commit", "--allow-empty", "-m", f"local {index}")
    assert git("rev-list", "--left-right", "--count", "origin/main...HEAD").split() == [
        str(behind),
        str(ahead),
    ]
    monkeypatch.setattr(
        status_mod,
        "probe_environment",
        lambda **kwargs: SimpleNamespace(capabilities=()),
    )
    args = [
        "--source",
        str(tmp_path),
        "status",
        "--profile=vm-headless",
        f"--config={config}",
    ]
    human = CliRunner().invoke(app, args)
    assert human.exit_code == 0, human.output
    assert ("in sync" in human.output) is (ahead == 0 and behind == 0)
    if behind:
        assert "behind" in human.output
    encoded = CliRunner().invoke(app, ["--format=json", *args])
    assert encoded.exit_code == 0, encoded.output
    info = json.loads(encoded.output)["data"]["config_repo"]
    assert info["commits_vs_origin"] == ahead
    assert info["commits_behind_origin"] == behind


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
    runner.add(("origin/main...HEAD",), returncode=0, stdout="0\t4\n")
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
    runner.add(("origin/main...HEAD",), returncode=0, stdout="0\t0\n")
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


def _patch_git_for_clean_repo(
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_cases: tuple[tuple[tuple[str, ...], int, str], ...] = (),
) -> _FakeGitRunner:
    """Install a happy-path git runner that resolves every status query.

    Tests that need ``status`` to issue git queries beyond the four
    happy-path cases (e.g. a ``<source_sha>..HEAD`` rev-list when a
    transition records a ``source_sha``) pass ``extra_cases`` as a
    tuple of ``(suffix, returncode, stdout)`` triples. They are added
    to the runner BEFORE :func:`subprocess.run` is patched so the
    runner is fully assembled up front — no post-patch reach-in.
    """
    runner = _FakeGitRunner()
    runner.add(("--is-inside-work-tree",), returncode=0, stdout="true\n")
    runner.add(("HEAD",), returncode=0, stdout="1f37cb1\n")
    runner.add(("origin/main",), returncode=0, stdout="abc\n")
    runner.add(("origin/main...HEAD",), returncode=0, stdout="0\t0\n")
    for suffix, returncode, stdout in extra_cases:
        runner.add(suffix, returncode=returncode, stdout=stdout)
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
    # source_sha = deadbeef triggers an extra `deadbeef..HEAD` git query;
    # register it up front so the runner is fully assembled before patch.
    _patch_git_for_clean_repo(
        monkeypatch,
        extra_cases=((("deadbeef..HEAD",), 0, "0\n"),),
    )

    result = _invoke_status(source_dir=tmp_path, config_path=config_path)

    assert result.exit_code == 0, result.output
    assert "=== setforge status — vm-headless" in result.output
    assert "config-repo:" in result.output
    assert "1f37cb1" in result.output
    assert "last install:" in result.output
    assert "drift:" in result.output
    assert "overlay:" in result.output
    assert "capabilities:" in result.output


def test_status_counts_drifted_selected_codex_instruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "tracked" / "codex" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    source.write_text("tracked instructions\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "AGENTS.md").write_text("live drift\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    _stub_transition(
        state_dir,
        profile="vm-headless",
        dirname="20260518T070015000000Z-install-vm-headless",
        source_sha="deadbeef",
    )
    _patch_git_for_clean_repo(
        monkeypatch,
        extra_cases=((("deadbeef..HEAD",), 0, "1\n"),),
    )

    result = _invoke_status(
        source_dir=tmp_path,
        config_path=_write_codex_instruction_config(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "drift:          1 drifted" in result.output
    assert "(install lag)" in result.output
    assert "(deployed current)" not in result.output


@pytest.mark.parametrize("live_state", ["drifted", "missing"])
def test_status_newer_head_retains_install_lag_when_deployment_not_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_state: str,
) -> None:
    """A newer HEAD is not deployed-current when any effective file is non-current."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_path = _write_minimal_config(tmp_path)
    if live_state == "drifted":
        live = home / ".local" / "share" / "setforge-test" / "doc.md"
        live.parent.mkdir(parents=True)
        live.write_text("locally changed\n", encoding="utf-8")

    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    _stub_transition(
        state_dir,
        profile="vm-headless",
        dirname="20260518T070015000000Z-install-vm-headless",
        source_sha="deadbeef",
    )
    _patch_git_for_clean_repo(
        monkeypatch,
        extra_cases=((("deadbeef..HEAD",), 0, "1\n"),),
    )

    result = _invoke_status(source_dir=tmp_path, config_path=config_path)

    assert result.exit_code == 0, result.output
    assert "(install lag)" in result.output
    assert "(deployed current)" not in result.output


def test_status_newer_head_does_not_claim_current_for_empty_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No compared entries cannot substantiate a deployed-current claim."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state_dir))
    _stub_transition(
        state_dir,
        profile="vm-headless",
        dirname="20260518T070015000000Z-install-vm-headless",
        source_sha="deadbeef",
    )
    _patch_git_for_clean_repo(
        monkeypatch,
        extra_cases=((("deadbeef..HEAD",), 0, "1\n"),),
    )

    result = _invoke_status(
        source_dir=tmp_path,
        config_path=_write_empty_config(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "(install lag)" in result.output
    assert "(deployed current)" not in result.output


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
    # source_sha = deadbeef triggers an extra `deadbeef..HEAD` git query;
    # register it up front so the runner is fully assembled before patch.
    _patch_git_for_clean_repo(
        monkeypatch,
        extra_cases=((("deadbeef..HEAD",), 0, "0\n"),),
    )

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


def _status_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Result, dict[str, object]]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config_path = _write_minimal_config(tmp_path)
    _patch_git_for_clean_repo(monkeypatch)
    result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "--source",
            str(tmp_path),
            "status",
            "--config",
            str(config_path),
            "--profile",
            "vm-headless",
        ],
    )
    assert result.exit_code == 0, result.output
    return result, json.loads(result.stdout)["data"]


def test_status_counts_missing_files_and_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, data = _status_json(tmp_path, monkeypatch)
    assert data["drift"] == {"drifted": 0, "missing": 1}
    assert data["pending_operation"] is None
    assert data["config_repo"]["commits_since_install_reason"] == "never installed"  # type: ignore[index]

    config_path = tmp_path / "setforge.yaml"
    human = _invoke_status(source_dir=tmp_path, config_path=config_path)
    assert "0 drifted, 1 missing" in human.output
    assert "=== ready" not in human.output
    assert "predates schema bump" not in human.output


def test_status_shows_unfinished_operation_with_recover_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = SimpleNamespace(command="install", operation_id="op-1")
    monkeypatch.setattr(status_mod.operations, "active", lambda profile: journal)
    _, data = _status_json(tmp_path, monkeypatch)
    assert data["pending_operation"] == {
        "command": "install",
        "operation_id": "op-1",
        "recover_command": "setforge recover --profile=vm-headless",
        "error": None,
    }

    human = _invoke_status(source_dir=tmp_path, config_path=tmp_path / "setforge.yaml")
    assert "unfinished install op-1" in human.output
    assert "setforge recover --profile=vm-headless" in human.output
    assert "=== ready" not in human.output
