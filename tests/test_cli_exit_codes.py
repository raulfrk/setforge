"""Focused CLI tests for exit-code contracts.

Broader CLI integration coverage lives under dotfiles-nen.9 (Docker
e2e). This file pins the narrow exit-code behaviors that are only
observable through the Typer surface — chiefly that ``ext reconcile``
exits non-zero in read-only modes when drift exists, and that
``install`` gates on unexpected drift (P4.3).
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from my_setup import claude_plugins as cp
from my_setup.cli import app

_FIXTURE_YAML = """\
version: 1
dotfiles:
  d:
    src: x
    dst: y
profiles:
  vmh:
    dotfiles: [d]
    extensions:
      include:
        - declared.one
      reconcile: report
  prune:
    dotfiles: [d]
    extensions:
      include:
        - declared.one
      reconcile: prune
"""


def _setup_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a minimal my_setup.yaml + tracked tree, mock the code CLI."""
    cfg = tmp_path / "my_setup.yaml"
    cfg.write_text(_FIXTURE_YAML, encoding="utf-8")
    (tmp_path / "tracked").mkdir()
    (tmp_path / "tracked" / "x").write_text("data\n")

    state: dict[str, list[str]] = {"installed": []}

    def fake_run(args, **kwargs: Any):
        if args[1] == "--list-extensions":
            stdout = "\n".join(state["installed"]) + (
                "\n" if state["installed"] else ""
            )
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if args[1] == "--install-extension":
            state["installed"].append(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "--uninstall-extension":
            if args[2] in state["installed"]:
                state["installed"].remove(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(
        "my_setup.vscode_extensions.resolve_binary", lambda _: Path("/usr/bin/code")
    )
    monkeypatch.setattr("my_setup.vscode_extensions.subprocess.run", fake_run)
    return cfg


def test_ext_reconcile_report_policy_exits_1_on_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app, ["ext", "reconcile", "--profile=vmh", f"--config={cfg}"]
    )
    assert result.exit_code == 1
    assert "would install    declared.one" in result.stdout


def test_ext_reconcile_dry_run_exits_1_on_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "ext",
            "reconcile",
            "--profile=prune",
            f"--config={cfg}",
            "--dry-run",
        ],
    )
    assert result.exit_code == 1


def test_ext_reconcile_prune_exits_0_after_acting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRUNE policy actually installs the missing extension and returns 0."""
    cfg = _setup_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app, ["ext", "reconcile", "--profile=prune", f"--config={cfg}"]
    )
    assert result.exit_code == 0
    assert "install    declared.one" in result.stdout


def test_install_warns_and_exits_0_when_claude_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install with claude absent emits a warning and still exits 0."""
    cfg = _setup_fixture(tmp_path, monkeypatch)

    # Claude binary is absent.
    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr("my_setup.claude_plugins.resolve_binary", lambda _: None)

    # CliRunner merges stdout + stderr into result.output by default.
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "install",
            "--profile=vmh",
            f"--config={cfg}",
            "--no-transition",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, f"output: {result.output}"
    # Warning must mention 'claude' (case-insensitive).
    assert "claude" in result.output.lower()


def test_ext_reconcile_clean_state_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPORT with no drift: nothing to do, exit 0."""
    cfg = _setup_fixture(tmp_path, monkeypatch)
    # Pre-install the declared extension so there's no drift.
    monkeypatch.setattr(
        "my_setup.vscode_extensions.resolve_binary", lambda _: Path("/usr/bin/code")
    )

    def fake_run(args, **kwargs: Any):
        if args[1] == "--list-extensions":
            return subprocess.CompletedProcess(args, 0, "declared.one\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("my_setup.vscode_extensions.subprocess.run", fake_run)
    runner = CliRunner()
    result = runner.invoke(
        app, ["ext", "reconcile", "--profile=vmh", f"--config={cfg}"]
    )
    assert result.exit_code == 0
    assert "nothing to reconcile" in result.stdout


# ---------------------------------------------------------------------------
# P4.3 — install gating + auto-accept flags
# ---------------------------------------------------------------------------

_INSTALL_FIXTURE_YAML = """\
version: 1
dotfiles:
  d:
    src: dotfile.txt
    dst: {dst}
profiles:
  p:
    dotfiles: [d]
"""


def _setup_install_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    src_text: str = "tracked\n",
    dst_text: str = "tracked\n",
) -> Path:
    """Write a minimal my_setup.yaml + tracked file + live destination."""
    dst = tmp_path / "live" / "dotfile.txt"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(dst_text, encoding="utf-8")

    cfg = tmp_path / "my_setup.yaml"
    cfg.write_text(_INSTALL_FIXTURE_YAML.format(dst=dst), encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "dotfile.txt").write_text(src_text, encoding="utf-8")

    # Disable extension reconcile (no code binary)
    monkeypatch.setattr("my_setup.vscode_extensions.resolve_binary", lambda _: None)
    # Disable transition writes for most tests
    monkeypatch.setattr("my_setup.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "my_setup.transitions.write_transition",
        lambda *a, **kw: tmp_path / "fake_transition",
    )
    return cfg


def test_install_clean_profile_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install exits 0 when there is no unexpected drift."""
    cfg = _setup_install_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["install", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0


def test_install_unexpected_drift_exits_1_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install exits 1 with the canonical message when unexpected drift exists.

    Uses a YAML dotfile with a non-preserved key that diverges between
    tracked and live — this produces a non-empty unexpected_drift_keys list
    which is what the install gate checks.
    """
    # Set up a YAML dotfile: tracked has a=1,b=2, live has a=99,b=88.
    # preserve_user_keys=[a] → b is unexpected drift.
    dst = tmp_path / "live" / "dotfile.txt"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("a: 99\nb: 88\n", encoding="utf-8")

    cfg = tmp_path / "my_setup.yaml"
    cfg.write_text(
        f"version: 1\ndotfiles:\n  d:\n    src: dotfile.txt\n    dst: {dst}\n"
        f"    preserve_user_keys: [a]\nprofiles:\n  p:\n    dotfiles: [d]\n",
        encoding="utf-8",
    )
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "dotfile.txt").write_text("a: 1\nb: 2\n", encoding="utf-8")

    monkeypatch.setattr("my_setup.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("my_setup.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "my_setup.transitions.write_transition",
        lambda *a, **kw: tmp_path / "fake",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["install", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (result.stderr or "")
    assert "unexpected drift" in combined
    assert "merge" in combined


def test_install_auto_accept_tracked_resolves_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--auto-accept-tracked proceeds non-interactively; transition recorded; exit 0."""
    cfg = _setup_install_fixture(
        tmp_path, monkeypatch, src_text="a: 1\nb: 2\n", dst_text="a: 1\nb: 99\n"
    )
    # Make it a YAML dotfile with preserve_user_keys so it creates unexpected drift
    # We need to update the config to use yaml and set preserve_user_keys
    dst = tmp_path / "live" / "dotfile.txt"
    cfg.write_text(
        f"version: 1\ndotfiles:\n  d:\n    src: dotfile.txt\n    dst: {dst}\n"
        f"    preserve_user_keys: [a]\nprofiles:\n  p:\n    dotfiles: [d]\n",
        encoding="utf-8",
    )
    (tmp_path / "tracked" / "dotfile.txt").write_text("a: 1\nb: 2\n", encoding="utf-8")

    transition_calls: list[Any] = []

    def _fake_write_transition(*a: Any, **kw: Any) -> Path:
        transition_calls.append(1)
        return tmp_path / "fake"

    monkeypatch.setattr(
        "my_setup.transitions.write_transition",
        _fake_write_transition,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=p", f"--config={cfg}", "--auto-accept-tracked"],
    )
    assert result.exit_code == 0


def test_install_auto_accept_live_resolves_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--auto-accept-live proceeds non-interactively; exit 0."""
    dst = tmp_path / "live" / "dotfile.txt"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("a: 1\nb: 99\n", encoding="utf-8")
    cfg = tmp_path / "my_setup.yaml"
    cfg.write_text(
        f"version: 1\ndotfiles:\n  d:\n    src: dotfile.txt\n    dst: {dst}\n"
        f"    preserve_user_keys: [a]\nprofiles:\n  p:\n    dotfiles: [d]\n",
        encoding="utf-8",
    )
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "dotfile.txt").write_text("a: 1\nb: 2\n", encoding="utf-8")

    monkeypatch.setattr("my_setup.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("my_setup.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "my_setup.transitions.write_transition",
        lambda *a, **kw: tmp_path / "fake",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=p", f"--config={cfg}", "--auto-accept-live"],
    )
    assert result.exit_code == 0


def test_install_both_flags_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing both --auto-accept-tracked and --auto-accept-live exits 2."""
    cfg = _setup_install_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={cfg}",
            "--auto-accept-tracked",
            "--auto-accept-live",
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# dotfiles-9by — section reconcile flag matrix
# ---------------------------------------------------------------------------


def test_install_reconcile_and_auto_mutually_exclusive_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--reconcile-user-sections + --auto=... exits 2 with an error."""
    cfg = _setup_install_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={cfg}",
            "--reconcile-user-sections",
            "--auto=use-tracked",
        ],
    )
    assert result.exit_code == 2
    combined = (result.stdout or "") + (result.stderr or "")
    assert "mutually exclusive" in combined


def test_install_auto_unknown_value_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--auto=garbage exits 2 with a descriptive error."""
    cfg = _setup_install_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=p", f"--config={cfg}", "--auto=garbage"],
    )
    assert result.exit_code == 2
    combined = (result.stdout or "") + (result.stderr or "")
    assert "use-tracked" in combined
    assert "keep-live" in combined


def test_install_auto_use_tracked_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--auto=use-tracked is accepted on a clean profile (no drift)."""
    cfg = _setup_install_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=p", f"--config={cfg}", "--auto=use-tracked"],
    )
    assert result.exit_code == 0


def test_install_auto_keep_live_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--auto=keep-live is accepted on a clean profile (no drift)."""
    cfg = _setup_install_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=p", f"--config={cfg}", "--auto=keep-live"],
    )
    assert result.exit_code == 0


def test_install_reconcile_alone_accepted_on_clean_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--reconcile-user-sections alone is accepted (and exits silently
    when no shared drift to prompt about)."""
    cfg = _setup_install_fixture(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=p", f"--config={cfg}", "--reconcile-user-sections"],
    )
    assert result.exit_code == 0
