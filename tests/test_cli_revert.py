"""End-to-end tests for transition recording and the revert command.

Uses Typer's CliRunner to drive the real CLI surface against a fixture
profile + tmp_path live tree, with subprocess.run mocked for the code CLI.
"""

import json
import subprocess
from pathlib import Path
from typing import Any, TypedDict

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.transitions import TransitionDir, load_filesystem_deltas


class _ExtState(TypedDict):
    """In-test mutable mock state for VSCode extension reconcile.

    ``installed`` is the running list of installed extension IDs;
    ``fail_uninstall`` is the set of IDs whose uninstall should raise
    :class:`subprocess.CalledProcessError` to simulate a failure.
    """

    installed: list[str]
    fail_uninstall: set[str]


_FIXTURE_YAML = """\
version: 1
tracked_files:
  greeting:
    src: greeting.md
    dst: {dst}
profiles:
  vmh:
    tracked_files: [greeting]
"""


def _setup_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Build a tracked/ tree + setforge.yaml at tmp_path. Returns (cfg, dst)."""
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    src = repo / "tracked" / "greeting.md"
    src.write_text("hello\n", encoding="utf-8")
    dst = tmp_path / "live" / "greeting.md"
    cfg = repo / "setforge.yaml"
    cfg.write_text(_FIXTURE_YAML.format(dst=dst), encoding="utf-8")
    return cfg, dst


def _state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `code` CLI absent (warn-and-skip for extension leg) without
    breaking lookups for other binaries."""
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary",
        lambda name: None,
    )


def test_install_writes_transition_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    result = CliRunner().invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert result.exit_code == 0, result.output

    transitions_dir = state / "transitions"
    assert transitions_dir.exists()
    children = list(transitions_dir.iterdir())
    assert len(children) == 1
    transition = children[0]
    assert (transition / "meta.json").exists()
    assert (transition / "filesystem_deltas.json").exists()
    # No extension delta when code CLI was absent.
    assert not (transition / "extensions.json").exists()

    meta = json.loads((transition / "meta.json").read_text())
    assert meta["command"] == "install"
    assert meta["profile"] == "vmh"


def test_install_no_transition_flag_skips_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _dst = _setup_repo(tmp_path)
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
    """A new dst file is recorded as absent before the install, so revert
    can delete it."""
    cfg, dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    assert not dst.exists()
    result = CliRunner().invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert result.exit_code == 0
    transition = next((state / "transitions").iterdir())
    recorded = {
        item.path: (item.pre.kind.value, item.post.payload)
        for item in load_filesystem_deltas(TransitionDir(transition))
    }
    assert recorded[dst] == ("absent", b"hello\n")


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

    result = runner.invoke(
        app,
        [
            "sync",
            "--profile=vmh",
            f"--config={cfg}",
            "--auto=use-live",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output

    children = list((state / "transitions").iterdir())
    assert len(children) == 1
    sync_transition = children[0]
    meta = json.loads((sync_transition / "meta.json").read_text())
    assert meta["command"] == "sync"
    recorded = load_filesystem_deltas(TransitionDir(sync_transition))
    # The src under tracked/ is what changed.
    assert [item.path.name for item in recorded] == ["greeting.md"]


def test_install_then_revert_restores_pre_install_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install creates a stub file; revert deletes it (round-trip via images)."""
    cfg, dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    install_result = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_result.exit_code == 0, install_result.output
    assert dst.exists()
    assert dst.read_text() == "hello\n"

    revert_result = runner.invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"]
    )
    assert revert_result.exit_code == 0, revert_result.output
    assert not dst.exists(), "stub file should be removed by revert"


def test_revert_with_no_history_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty history → non-zero exit with NoTransitionFound. CliRunner
    bypasses the CLI's main() error wrapper, so the exit comes via the
    raised exception rather than printed output; assert on both."""
    from setforge.errors import NoTransitionFound

    cfg, _ = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    result = CliRunner().invoke(app, ["revert", "--profile=vmh", f"--config={cfg}"])
    assert result.exit_code == 1
    assert isinstance(result.exception, NoTransitionFound)
    assert "no transition history" in str(result.exception)


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

    # Patch the fixture YAML to declare an extension include list via the
    # top-level packages registry + a profile packages ref (the post-contract
    # surface for what used to be a profile ``extensions.include`` block).
    yaml = cfg.read_text(encoding="utf-8")
    yaml = yaml.replace(
        "profiles:\n",
        "packages:\n"
        "  example.ext-a:\n"
        "    type: extension\n"
        "    extension: example.ext-a\n"
        "  example.ext-b:\n"
        "    type: extension\n"
        "    extension: example.ext-b\n"
        "profiles:\n",
    )
    yaml = yaml.replace(
        "    tracked_files: [greeting]\n",
        "    tracked_files: [greeting]\n    packages: [example.ext-a, example.ext-b]\n",
    )
    cfg.write_text(yaml, encoding="utf-8")

    state: dict[str, list[str]] = {"installed": []}
    real_run = subprocess.run

    def fake_run(args, **kwargs: Any):
        # Intercept only `code` invocations; let everything else hit the
        # real binary.
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
        "setforge.vscode_extensions.resolve_binary",
        lambda name: Path("/usr/bin/code") if name == "code" else None,
    )
    monkeypatch.setattr("setforge.vscode_extensions.subprocess.run", fake_run)

    pre_install = sorted(state["installed"])

    runner = CliRunner()
    install_result = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_result.exit_code == 0, install_result.output
    assert sorted(state["installed"]) == [
        "example.ext-a",
        "example.ext-b",
    ]

    revert_result = runner.invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"]
    )
    assert revert_result.exit_code == 0, revert_result.output

    # End-state assertion: extension set is back to pre-install bytes.
    assert sorted(state["installed"]) == pre_install
    # And the file revert also took effect.
    assert not dst.exists()


def test_revert_refuses_when_target_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a touched file has drifted since the transition was recorded,
    revert refuses with a non-zero exit and no partial changes."""
    from setforge.errors import RevertFailed

    cfg, dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])

    # Drift the live file so it no longer holds what the install left.
    dst.write_text("manually edited content\n", encoding="utf-8")
    drifted_content = dst.read_text()

    result = runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"])
    assert result.exit_code == 1
    assert isinstance(result.exception, RevertFailed)
    # Drifted content survived — no partial revert.
    assert dst.read_text() == drifted_content


_TREE_FIXTURE_YAML = """\
schema_version: '6.2'
minimum_version: '6.2'
version: 1
tracked_files:
  greeting:
    src: greeting.md
    dst: {dst}
  bundle:
    src: bundle
    dst: {tree}
    tree: {{}}
profiles:
  vmh:
    tracked_files: [greeting, bundle]
"""


def test_refused_revert_of_guarded_transition_leaves_no_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import operations
    from setforge.errors import RevertFailed

    cfg, dst = _setup_repo(tmp_path)
    bundle = cfg.parent / "tracked" / "bundle"
    bundle.mkdir()
    (bundle / "item.txt").write_text("item\n", encoding="utf-8")
    cfg.write_text(
        _TREE_FIXTURE_YAML.format(dst=dst, tree=tmp_path / "trees" / "bundle"),
        encoding="utf-8",
    )
    (tmp_path / "trees").mkdir()
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    runner = CliRunner()
    args = ["--profile=vmh", f"--config={cfg}"]
    installed = runner.invoke(app, ["install", *args, "--yes"])
    assert installed.exit_code == 0, installed.output
    dst.write_text("manually edited content\n", encoding="utf-8")

    result = runner.invoke(app, ["revert", *args, "--yes"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RevertFailed)
    assert not getattr(result.exception, "__notes__", ())
    assert dst.read_text(encoding="utf-8") == "manually edited content\n"
    assert operations.active("vmh") is None
    operations.refuse_pending()


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

    runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"])
    assert not dst.exists()

    redo = runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"])
    assert redo.exit_code == 0, redo.output
    assert dst.read_text() == "hello\n"


def test_revert_continues_after_extension_uninstall_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ExtensionInstallFailed during revert's uninstall loop must not
    abort revert. Other extensions continue, and the reverse transition
    is still written so the user has a redo path."""
    cfg, _dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)

    yaml = cfg.read_text(encoding="utf-8")
    yaml = yaml.replace(
        "profiles:\n",
        "packages:\n"
        "  good.one:\n"
        "    type: extension\n"
        "    extension: good.one\n"
        "  broken.one:\n"
        "    type: extension\n"
        "    extension: broken.one\n"
        "profiles:\n",
    )
    yaml = yaml.replace(
        "    tracked_files: [greeting]\n",
        "    tracked_files: [greeting]\n    packages: [good.one, broken.one]\n",
    )
    cfg.write_text(yaml, encoding="utf-8")

    state_ext: _ExtState = {"installed": [], "fail_uninstall": set()}
    real_run = subprocess.run

    def fake_run(args, **kwargs: Any):
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
        "setforge.vscode_extensions.resolve_binary",
        lambda name: Path("/usr/bin/code") if name == "code" else None,
    )
    monkeypatch.setattr("setforge.vscode_extensions.subprocess.run", fake_run)

    runner = CliRunner()
    install_result = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_result.exit_code == 0, install_result.output
    assert sorted(state_ext["installed"]) == ["broken.one", "good.one"]

    # Wire the failure for the reverse uninstall.
    state_ext["fail_uninstall"].add("broken.one")

    revert_result = runner.invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"]
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


# ---------------------------------------------------------------------------
# sqcw: --to-before=<id> multi-step atomic revert
# ---------------------------------------------------------------------------


def _two_install_sequence(cfg: Path, runner: CliRunner) -> tuple[Path, Path, Path]:
    """Run install twice with a content change between them.

    Returns ``(dst, transition_a_dir, transition_b_dir)`` in chronological
    order so callers can target the oldest transition with ``--to-before``
    and assert the chain unwinds both steps.
    """
    install_result = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_result.exit_code == 0, install_result.output

    # Edit the tracked src so the second install records a content delta
    # (modifies the live file from "hello\n" to "hello world\n").
    src = cfg.parent / "tracked" / "greeting.md"
    src.write_text("hello world\n", encoding="utf-8")
    install_result_b = runner.invoke(
        app, ["install", "--profile=vmh", f"--config={cfg}"]
    )
    assert install_result_b.exit_code == 0, install_result_b.output

    # Resolve transitions chronologically.
    state = Path(__import__("os").environ["SETFORGE_STATE_DIR"])
    transition_dirs = sorted(d for d in (state / "transitions").iterdir() if d.is_dir())
    # 2 install transitions land first.
    install_transitions = [d for d in transition_dirs if "install" in d.name]
    assert len(install_transitions) == 2
    return (
        cfg.parent.parent / "live" / "greeting.md",
        install_transitions[0],
        install_transitions[1],
    )


def test_revert_to_before_two_step_unwinds_chain_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two install transitions; --to-before=<oldest> unwinds both
    transitions in reverse-chronological order.

    Asserts:
    - exit 0, no .rej siblings, both reverse transitions recorded;
    - live file rolled back to pre-install (does not exist);
    - via a call log: the whole chain is checked against live (before the
      wizard and again under the locks) BEFORE any step is applied, and the
      steps apply newest-first, each re-checking its own files.
    """
    cfg, dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    live, transition_a, transition_b = _two_install_sequence(cfg, runner)
    assert live.exists()

    from setforge import transitions as _transitions_module
    from setforge.cli import revert as _revert_module

    call_log: list[tuple[str, object]] = []
    real_check = _transitions_module.validate_filesystem_deltas_reverse
    real_apply = _revert_module._apply_revert

    def _logging_check(*steps: Any) -> None:
        call_log.append(("check", len(steps)))
        real_check(*steps)

    def _logging_apply(record: Any, *args: Any, **kwargs: Any) -> Path:
        call_log.append(("apply", record.directory))
        return real_apply(record, *args, **kwargs)

    monkeypatch.setattr(
        _transitions_module, "validate_filesystem_deltas_reverse", _logging_check
    )
    monkeypatch.setattr(_revert_module, "_apply_revert", _logging_apply)

    revert_result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={transition_a.name}",
            "--yes",
        ],
    )
    assert revert_result.exit_code == 0, revert_result.output

    # Two reverse transitions were recorded — one per real-apply step.
    state_dir = Path(__import__("os").environ["SETFORGE_STATE_DIR"])
    revert_dirs = [
        d for d in (state_dir / "transitions").iterdir() if "-revert-vmh" in d.name
    ]
    assert len(revert_dirs) == 2

    # Live file is gone (rolled all the way back to pre-install).
    assert not dst.exists()
    # No .rej leakage anywhere.
    assert list(tmp_path.rglob("*.rej")) == []

    # The whole chain is checked before any step is applied, and the chain
    # unwinds newest-first.
    assert call_log == [
        ("check", 2),
        ("check", 2),
        ("apply", transition_b),
        ("check", 1),
        ("apply", transition_a),
        ("check", 1),
    ]


def test_revert_to_before_dry_run_failure_aborts_with_no_live_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the dry-run pass fails on ANY step, the chain aborts with NO
    live mutations — the file stays drifted."""
    cfg, _dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    live, transition_a, _transition_b = _two_install_sequence(cfg, runner)
    pre = live.read_text()
    # Drift the live file so the dry-run will fail.
    live.write_text("manually edited\n", encoding="utf-8")
    drifted = live.read_text()

    revert_result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={transition_a.name}",
            "--yes",
        ],
    )
    # Non-zero exit; live unchanged.
    assert revert_result.exit_code == 1
    assert live.read_text() == drifted
    assert live.read_text() != pre
    # No reverse transitions written — the dry-run pass aborted first.
    state_dir = Path(__import__("os").environ["SETFORGE_STATE_DIR"])
    revert_dirs = [
        d for d in (state_dir / "transitions").iterdir() if "-revert-vmh" in d.name
    ]
    assert revert_dirs == []


def test_revert_to_before_reports_no_recorded_revert_for_a_rolled_back_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import operations
    from setforge.errors import RevertFailed

    cfg, _dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    runner = CliRunner()
    live, transition_a, _transition_b = _two_install_sequence(cfg, runner)
    real_apply = operations.apply_filesystem_deltas_reverse_anchored
    applied: list[object] = []

    def fail_second_step(*args: Any, **kwargs: Any) -> None:
        applied.append(args)
        if len(applied) == 2:
            raise RevertFailed("second step failed")
        real_apply(*args, **kwargs)

    monkeypatch.setattr(
        operations, "apply_filesystem_deltas_reverse_anchored", fail_second_step
    )

    result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={transition_a.name}",
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert live.read_text(encoding="utf-8") == "hello world\n"
    assert [d for d in (state / "transitions").iterdir() if "-revert-" in d.name] == []
    assert operations.active("vmh") is None
    assert "transition: " not in result.output
    assert "to REDO this revert" not in result.output
    assert "rolled back 1 already reverted step(s) of this chain" in result.output


def test_revert_to_before_reports_each_recorded_revert_after_the_chain_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _dst = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    runner = CliRunner()
    _live, transition_a, _transition_b = _two_install_sequence(cfg, runner)

    result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={transition_a.name}",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    recorded = sorted(
        d for d in (state / "transitions").iterdir() if "-revert-" in d.name
    )
    lines = result.output.splitlines()
    reported = [line for line in lines if line.startswith("transition: ")]
    assert reported == [f"transition: {path}" for path in recorded]
    last_progress = max(i for i, line in enumerate(lines) if "reverting: " in line)
    assert lines.index(reported[0]) > last_progress


def test_revert_help_describes_an_all_or_nothing_chain() -> None:
    result = CliRunner().invoke(app, ["revert", "--help"])

    assert result.exit_code == 0
    assert "partial" not in result.output
    assert "rolled back" in " ".join(result.output.split())


def test_revert_to_before_single_target_acts_like_bare_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When --to-before points at the most-recent transition, the chain
    is length-1 and behaves identically to bare ``revert``."""
    cfg, dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    install_result = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_result.exit_code == 0
    assert dst.exists()

    state_dir = Path(__import__("os").environ["SETFORGE_STATE_DIR"])
    transition = next(
        d for d in (state_dir / "transitions").iterdir() if "install" in d.name
    )
    revert_result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={transition.name}",
            "--yes",
        ],
    )
    assert revert_result.exit_code == 0, revert_result.output
    assert not dst.exists()
    # Exactly one reverse transition.
    revert_dirs = [
        d for d in (state_dir / "transitions").iterdir() if "-revert-vmh" in d.name
    ]
    assert len(revert_dirs) == 1


def test_revert_to_before_resolves_prefix_to_full_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--to-before accepts a unique-prefix match per resolve_transition_prefix.
    Ambiguous prefix surfaces the error from resolve_transition_prefix."""
    cfg, _ = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    # Need to install to produce a transition we can address.
    install_result = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_result.exit_code == 0
    transition_dir = next(
        d for d in (state / "transitions").iterdir() if "install" in d.name
    )

    # First 12 chars of the YYYYMMDDTHHMMSS prefix should be unique
    # (only one transition exists).
    prefix = transition_dir.name[:12]
    revert_result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={prefix}",
            "--yes",
        ],
    )
    assert revert_result.exit_code == 0, revert_result.output


def test_revert_to_before_user_aborts_via_radiolist_makes_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-step revert wizard returning ABORT must leave the live tree
    and the transitions history untouched.

    Mocks ``setforge.cli._revert_confirm.button_bar`` to return
    :class:`RevertChoice.ABORT`, drives ``revert --to-before`` (no
    ``--yes``) under an isatty stub, and asserts exit 0 + no live
    mutation + no reverse-transition dir.
    """
    cfg, _dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    live, transition_a, _transition_b = _two_install_sequence(cfg, runner)
    assert live.exists()
    pre = live.read_text(encoding="utf-8")

    state_dir = Path(__import__("os").environ["SETFORGE_STATE_DIR"])
    pre_revert_dirs = [
        d for d in (state_dir / "transitions").iterdir() if "-revert-vmh" in d.name
    ]
    assert pre_revert_dirs == []

    # Make the wizard think it's interactive (typer's CliRunner swaps
    # sys.stdin to a non-TTY pipe AT invoke time, so we cannot just
    # patch the underlying sys.stdin). Swap the entire `sys` module
    # reference inside _revert_confirm with a stub that exposes a TTY
    # stdin. Then have button_bar return ABORT.
    from setforge.cli import _revert_confirm as _rc_module
    from setforge.cli._revert_confirm import RevertChoice as _RC

    class _FakeStdin:
        @staticmethod
        def isatty() -> bool:
            return True

    class _FakeSys:
        stdin = _FakeStdin()

    monkeypatch.setattr(_rc_module, "sys", _FakeSys)

    def _fake_button_bar(*_args: object, **_kwargs: object) -> object:
        return _RC.ABORT

    monkeypatch.setattr("setforge.cli._revert_confirm.button_bar", _fake_button_bar)

    revert_result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={transition_a.name}",
        ],
    )
    assert revert_result.exit_code == 0, revert_result.output

    # Live unchanged.
    assert live.exists()
    assert live.read_text(encoding="utf-8") == pre
    # No reverse transition recorded.
    post_revert_dirs = [
        d for d in (state_dir / "transitions").iterdir() if "-revert-vmh" in d.name
    ]
    assert post_revert_dirs == []


def test_revert_to_before_wrong_profile_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If --to-before points at a transition recorded under a different
    profile, refuse rather than silently revert another profile's state."""
    from setforge.errors import SetforgeError

    cfg, _ = _setup_repo(tmp_path)
    state = _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    # Manually plant a different-profile transition.
    other = state / "transitions" / "20300101T000000000000Z-install-other"
    other.mkdir(parents=True)
    (other / "meta.json").write_text(
        json.dumps(
            {
                "command": "install",
                "profile": "other",
                "timestamp": "2030-01-01T00:00:00+00:00",
                "host": "h",
                "version": "0.1.0",
            }
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    revert_result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={other.name}",
            "--yes",
        ],
    )
    assert revert_result.exit_code == 1
    assert isinstance(revert_result.exception, SetforgeError)
    assert "for profile 'other'" in str(revert_result.exception)


@pytest.mark.parametrize(
    ("pre", "post", "expected"),
    [
        (b"keep line\n-- foo\n", b"keep line\nadded line\n", ("M", "+1 -1")),
        (b"--- a\n+++ b\n", b"--- a\n+++ b\n@@ c\n", ("M", "+1 -0")),
        (None, b"one\ntwo", ("+", "+2 -0")),
        (b"one\n", None, ("-", "+0 -1")),
        (b"a\r\nb", b"a\r\nb\n", ("M", "+1 -1")),
    ],
)
def test_file_change_marks_and_counts_lines_of_the_images(
    pre: bytes | None, post: bytes | None, expected: tuple[str, str]
) -> None:
    from setforge.cli.revert import _file_change
    from setforge.transitions import FilesystemDelta, FilesystemImage, FilesystemKind

    def image(payload: bytes | None) -> FilesystemImage:
        if payload is None:
            return FilesystemImage(FilesystemKind.ABSENT)
        return FilesystemImage(FilesystemKind.FILE, payload, mode=0o644, mtime_ns=0)

    assert _file_change(FilesystemDelta(Path("/x"), image(pre), image(post))) == (
        expected
    )


def test_file_change_of_a_symlink_has_no_line_counts() -> None:
    from setforge.cli.revert import _file_change
    from setforge.transitions import FilesystemDelta, FilesystemImage, FilesystemKind

    link = FilesystemImage(
        FilesystemKind.SYMLINK, link_target="t", mode=0o777, mtime_ns=0
    )

    assert _file_change(
        FilesystemDelta(Path("/x"), FilesystemImage(FilesystemKind.ABSENT), link)
    ) == ("+", "")


@pytest.mark.parametrize(
    "payload",
    [b"\xff\xfe\x00not utf-8\x80", b'{"paths": [', b"[]"],
    ids=["not-utf8", "truncated", "not-an-object"],
)
def test_revert_refuses_unreadable_transition_meta_cleanly(
    tmp_path: Path, payload: bytes
) -> None:
    from setforge import transitions
    from setforge.errors import InvalidTransitionRecord

    transition = transitions.TransitionDir(tmp_path / "20260101T000000Z-install-vmh")
    transition.mkdir()
    (transition / "meta.json").write_bytes(payload)

    with pytest.raises(InvalidTransitionRecord, match=r"meta\.json"):
        transitions.load_record(transition)


def test_file_change_counts_a_large_repetitive_file_quickly() -> None:
    """The line count is a display summary; thousands of near-identical lines
    must not stall the preview or ``transitions show``."""
    import time

    from setforge.cli.revert import _file_change
    from setforge.transitions import FilesystemDelta, FilesystemImage, FilesystemKind

    pre = b"".join(b'  "key": "value",\n' for _ in range(6000))
    post = pre.replace(b'"value",\n', b'"other",\n', 3000)

    def image(payload: bytes) -> FilesystemImage:
        return FilesystemImage(FilesystemKind.FILE, payload, mode=0o644, mtime_ns=0)

    started = time.monotonic()
    marker, counts = _file_change(FilesystemDelta(Path("/x"), image(pre), image(post)))

    assert time.monotonic() - started < 2
    assert marker == "M"
    assert counts.startswith("+")
