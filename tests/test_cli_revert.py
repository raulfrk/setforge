"""End-to-end tests for transition recording and the revert command.

Uses Typer's CliRunner to drive the real CLI surface against a fixture
profile + tmp_path live tree, with subprocess.run mocked for the code CLI.
"""

import json
import shutil
import subprocess
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
    """Make `code` CLI absent (warn-and-skip for extension leg) without
    breaking lookups for other binaries (e.g. `patch` for revert).

    ``vscode_extensions.resolve_binary`` and ``transitions.resolve_binary``
    are distinct module attributes even though they reference the same
    function; patching one leaves the other free to hit real PATH.
    """
    monkeypatch.setattr(
        "my_setup.vscode_extensions.resolve_binary",
        lambda name: None,
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
    # Paths are root-relative (no leading /) so GNU patch's safe-paths
    # check passes when revert applies with `-d /`.
    assert str(dst).lstrip("/") in patch


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


@pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)
def test_install_then_revert_restores_pre_install_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install creates a stub file; revert deletes it (round-trip via patch -R)."""
    cfg, dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    install_result = runner.invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}"]
    )
    assert install_result.exit_code == 0, install_result.output
    assert dst.exists()
    assert dst.read_text() == "hello\n"

    revert_result = runner.invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}"]
    )
    assert revert_result.exit_code == 0, revert_result.output
    assert not dst.exists(), "stub file should be removed by revert"


def test_revert_with_no_history_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty history → non-zero exit with NoTransitionFound. CliRunner
    bypasses the CLI's main() error wrapper, so the exit comes via the
    raised exception rather than printed output; assert on both."""
    from my_setup.errors import NoTransitionFound

    cfg, _ = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    result = CliRunner().invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}"]
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, NoTransitionFound)
    assert "no transition history" in str(result.exception)


@pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)
def test_revert_restores_extension_state_to_pre_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-state assertion: after revert, the fake installed-extension set
    must match its pre-install state — not just files.

    Drives a fake `code` CLI that mutates an in-memory installed list,
    runs install (which adds extensions), then revert (which must remove
    them). The final installed set is asserted byte-equal to pre-install.
    """
    cfg, dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)

    # Patch the fixture YAML to declare an extension include list.
    yaml = cfg.read_text(encoding="utf-8")
    yaml = yaml.replace(
        "    dotfiles: [greeting]\n",
        "    dotfiles: [greeting]\n"
        "    extensions:\n"
        "      include:\n"
        "        - example.ext-a\n"
        "        - example.ext-b\n",
    )
    cfg.write_text(yaml, encoding="utf-8")

    state = {"installed": []}
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        # Intercept only `code` invocations; let everything else (notably
        # `patch -R` from apply_patch_reverse) hit the real binary.
        if args[0] != "/usr/bin/code":
            return real_run(args, **kwargs)
        if args[1] == "--list-extensions":
            stdout = "\n".join(state["installed"]) + (
                "\n" if state["installed"] else ""
            )
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if args[1] == "--install-extension":
            ext_id = args[2]
            if ext_id not in state["installed"]:
                state["installed"].append(ext_id)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "--uninstall-extension":
            ext_id = args[2]
            if ext_id in state["installed"]:
                state["installed"].remove(ext_id)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(
        "my_setup.vscode_extensions.resolve_binary",
        lambda name: Path("/usr/bin/code") if name == "code" else None,
    )
    monkeypatch.setattr("my_setup.vscode_extensions.subprocess.run", fake_run)

    pre_install = sorted(state["installed"])

    runner = CliRunner()
    install_result = runner.invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}"]
    )
    assert install_result.exit_code == 0, install_result.output
    assert sorted(state["installed"]) == [
        "example.ext-a",
        "example.ext-b",
    ]

    revert_result = runner.invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}"]
    )
    assert revert_result.exit_code == 0, revert_result.output

    # End-state assertion: extension set is back to pre-install bytes.
    assert sorted(state["installed"]) == pre_install
    # And the file revert also took effect.
    assert not dst.exists()


@pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)
def test_revert_refuses_when_target_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a touched file has drifted since the transition was recorded,
    revert refuses with a non-zero exit and no partial changes."""
    from my_setup.errors import RevertFailed

    cfg, dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])

    # Drift the live file so patch -R can't reverse it cleanly.
    dst.write_text("manually edited content\n", encoding="utf-8")
    drifted_content = dst.read_text()

    result = runner.invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}"]
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, RevertFailed)
    # Drifted content survived — no partial revert.
    assert dst.read_text() == drifted_content


@pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)
def test_install_revert_revert_restores_install_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """revert records its own transition, so revert-of-revert ('redo')
    restores the post-install state."""
    cfg, dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert dst.read_text() == "hello\n"

    runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}"])
    assert not dst.exists()

    redo = runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}"])
    assert redo.exit_code == 0, redo.output
    assert dst.read_text() == "hello\n"


@pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)
def test_revert_continues_after_extension_uninstall_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ExtensionInstallFailed during revert's uninstall loop must not
    abort revert. Other extensions continue, and the reverse transition
    is still written so the user has a redo path."""
    cfg, dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)

    yaml = cfg.read_text(encoding="utf-8")
    yaml = yaml.replace(
        "    dotfiles: [greeting]\n",
        "    dotfiles: [greeting]\n"
        "    extensions:\n"
        "      include:\n"
        "        - good.one\n"
        "        - broken.one\n",
    )
    cfg.write_text(yaml, encoding="utf-8")

    state_ext = {"installed": [], "fail_uninstall": set()}
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[0] != "/usr/bin/code":
            return real_run(args, **kwargs)
        if args[1] == "--list-extensions":
            stdout = "\n".join(state_ext["installed"]) + (
                "\n" if state_ext["installed"] else ""
            )
            return subprocess.CompletedProcess(args, 0, stdout, "")
        if args[1] == "--install-extension":
            state_ext["installed"].append(args[2])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "--uninstall-extension":
            ext_id = args[2]
            if ext_id in state_ext["fail_uninstall"]:
                raise subprocess.CalledProcessError(
                    1, args, output="", stderr="simulated failure"
                )
            if ext_id in state_ext["installed"]:
                state_ext["installed"].remove(ext_id)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(
        "my_setup.vscode_extensions.resolve_binary",
        lambda name: Path("/usr/bin/code") if name == "code" else None,
    )
    monkeypatch.setattr("my_setup.vscode_extensions.subprocess.run", fake_run)

    runner = CliRunner()
    install_result = runner.invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}"]
    )
    assert install_result.exit_code == 0, install_result.output
    assert sorted(state_ext["installed"]) == ["broken.one", "good.one"]

    # Wire the failure for the reverse uninstall.
    state_ext["fail_uninstall"].add("broken.one")

    revert_result = runner.invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}"]
    )
    assert revert_result.exit_code == 0, revert_result.output

    # `good.one` got uninstalled despite `broken.one`'s failure.
    assert "good.one" not in state_ext["installed"]
    # `broken.one` is still installed (uninstall failed).
    assert "broken.one" in state_ext["installed"]
    # Reverse transition was still written (redo path preserved).
    transitions_dir = list((state / "transitions").iterdir())
    revert_dirs = [d for d in transitions_dir if "revert" in d.name]
    assert len(revert_dirs) == 1
    # FAILED is surfaced in stderr (CliRunner mixes by default).
    assert "FAILED" in revert_result.output


