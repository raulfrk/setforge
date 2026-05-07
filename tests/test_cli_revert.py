"""End-to-end tests for transition recording and the revert command.

Uses Typer's CliRunner to drive the real CLI surface against a fixture
profile + tmp_path live tree, with subprocess.run mocked for the code CLI.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_setup.cli import app


_FIXTURE_YAML = """\
version: 1
dotfiles:
  greeting:
    src: greeting.md
    dst: {dst}
profiles:
  vmh:
    dotfiles: [greeting]
"""


def _setup_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Build a tracked/ tree + my_setup.yaml at tmp_path. Returns (cfg, dst)."""
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    src = repo / "tracked" / "greeting.md"
    src.write_text("hello\n", encoding="utf-8")
    dst = tmp_path / "live" / "greeting.md"
    cfg = repo / "my_setup.yaml"
    cfg.write_text(_FIXTURE_YAML.format(dst=dst), encoding="utf-8")
    return cfg, dst


def _state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("MY_SETUP_STATE_DIR", str(state))
    return state


def _no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make code CLI absent so install skips the extension leg cleanly."""
    monkeypatch.setattr(
        "my_setup.extensions.shutil.which", lambda _: None
    )


def test_install_writes_transition_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    result = CliRunner().invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output

    transitions_dir = state / "transitions"
    assert transitions_dir.exists()
    children = list(transitions_dir.iterdir())
    assert len(children) == 1
    transition = children[0]
    assert (transition / "meta.json").exists()
    assert (transition / "changes.patch").exists()
    # No extension delta when code CLI was absent.
    assert not (transition / "extensions.json").exists()

    meta = json.loads((transition / "meta.json").read_text())
    assert meta["command"] == "install"
    assert meta["profile"] == "vmh"


def test_install_no_transition_flag_skips_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["install", "--profile=vmh", f"--config={cfg}", "--no-transition"],
    )
    assert result.exit_code == 0, result.output
    assert not (state / "transitions").exists()


def test_install_transition_records_stub_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new dst file shows up as `/dev/null -> path` in changes.patch,
    so revert can delete it."""
    cfg, dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    assert not dst.exists()
    result = CliRunner().invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}"]
    )
    assert result.exit_code == 0
    transition = next((state / "transitions").iterdir())
    patch = (transition / "changes.patch").read_text()
    assert "/dev/null" in patch
    assert str(dst) in patch


def test_sync_writes_transition_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    install_result = runner.invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}", "--no-transition"]
    )
    assert install_result.exit_code == 0, install_result.output
    dst.write_text("hello edited\n", encoding="utf-8")

    result = runner.invoke(app, ["sync", "--profile=vmh", f"--config={cfg}"])
    assert result.exit_code == 0, result.output

    children = list((state / "transitions").iterdir())
    assert len(children) == 1
    sync_transition = children[0]
    meta = json.loads((sync_transition / "meta.json").read_text())
    assert meta["command"] == "sync"
    patch = (sync_transition / "changes.patch").read_text()
    # The src under tracked/ is what changed.
    assert "greeting.md" in patch
