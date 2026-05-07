"""Focused CLI tests for exit-code contracts.

Broader CLI integration coverage lives under dotfiles-nen.9 (Docker
e2e). This file pins the narrow exit-code behaviors that are only
observable through the Typer surface — chiefly that ``ext reconcile``
exits non-zero in read-only modes when drift exists.
"""

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

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

    state = {"installed": []}

    def fake_run(args, **kwargs):
        if args[1] == "--list-extensions":
            stdout = "\n".join(state["installed"]) + ("\n" if state["installed"] else "")
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
        "my_setup.extensions.resolve_binary", lambda _: Path("/usr/bin/code")
    )
    monkeypatch.setattr("my_setup.extensions.subprocess.run", fake_run)
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
    monkeypatch.setattr("my_setup.claude_plugins._claude_bin", None)
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
        "my_setup.extensions.resolve_binary", lambda _: Path("/usr/bin/code")
    )

    def fake_run(args, **kwargs):
        if args[1] == "--list-extensions":
            return subprocess.CompletedProcess(args, 0, "declared.one\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("my_setup.extensions.subprocess.run", fake_run)
    runner = CliRunner()
    result = runner.invoke(
        app, ["ext", "reconcile", "--profile=vmh", f"--config={cfg}"]
    )
    assert result.exit_code == 0
    assert "nothing to reconcile" in result.stdout
