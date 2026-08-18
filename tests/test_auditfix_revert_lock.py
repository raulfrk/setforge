"""Regression: ``revert`` must mutate live state under ``profile_lock``.

``install`` (install.py) and ``sync`` (sync.py) perform their mutations
inside ``with profile_lock(profile):``. The deploy model relies on a
single-serialized-process assumption to justify the resolve->write
staleness window and the symlink-ordering window. ``revert`` previously
mutated live files (``patch -R``), restored store state, unlinked
symlinks, and appended its own reverse transition entirely UNLOCKED — so
a concurrent install/sync/revert could interleave and corrupt the live
tree and/or the recorded base.

These tests assert that revert enters ``profile_lock`` BEFORE any mutating
operation, for both the single-step and ``--to-before`` multi-step paths.
They fail against the old (unlocked) behavior: the lock was never entered,
so the recorded order never contains an ``enter`` event before ``apply``.
"""

import contextlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import transitions
from setforge.cli import app

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
    """Make the `code` CLI absent so the extension leg warn-and-skips."""
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary",
        lambda name: None,
    )


def _install_recording_lock(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``mutation_locks`` + patch reversal to record call order.

    Returns the shared event list. The recording lock wraps the real
    :func:`setforge.locking.mutation_locks`, appending ``"enter"`` /
    ``"exit"`` markers around it so the lock's serialization is exercised
    for real while the order is observable. ``apply_patch_reverse`` is
    wrapped to append ``"apply"`` before delegating to the real impl.
    """
    import setforge.transitions as transitions_module
    from setforge import locking
    from setforge.transitions import TransitionDir

    events: list[str] = []
    real_lock = locking.mutation_locks
    real_apply = transitions_module.apply_patch_reverse

    @contextlib.contextmanager
    def recording_lock(**scopes: object):
        events.append("enter")
        with real_lock(**scopes):  # type: ignore[arg-type]
            try:
                yield
            finally:
                events.append("exit")

    def recording_apply(
        transition_dir: TransitionDir, *, dry_run: bool = False
    ) -> None:
        # Only the real (mutating) reversal counts; the multi-step
        # pre-flight passes dry_run=True and runs before the lock.
        if not dry_run:
            events.append("apply")
        real_apply(transition_dir, dry_run=dry_run)

    monkeypatch.setattr("setforge.cli.revert.mutation_locks", recording_lock)
    monkeypatch.setattr(transitions_module, "apply_patch_reverse", recording_apply)
    return events


def test_single_step_revert_holds_lock_before_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare ``revert`` must enter profile_lock before any patch-reverse."""
    cfg, _dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    install_result = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_result.exit_code == 0, install_result.output

    events = _install_recording_lock(monkeypatch)
    revert_result = runner.invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"]
    )
    assert revert_result.exit_code == 0, revert_result.output

    assert "enter" in events, "revert never acquired the profile lock"
    assert "apply" in events, "revert never performed a patch-reverse"
    assert events.index("enter") < events.index("apply"), (
        f"lock must be held before mutating; observed order: {events}"
    )
    assert events[-1] == "exit", f"lock must be released last; order: {events}"


def test_revert_apply_failure_restores_real_cli_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compensatable revert failure restores files and clears its journal."""
    cfg, dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    runner = CliRunner()
    installed = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert installed.exit_code == 0, installed.output
    before = dst.read_bytes()

    def fail_after_write(*_args: object, **_kwargs: object) -> None:
        dst.write_text("partial revert\n", encoding="utf-8")
        raise RuntimeError("revert interrupted")

    monkeypatch.setattr("setforge.cli.revert._apply_revert", fail_after_write)

    result = runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"])

    assert result.exit_code == 1
    assert dst.read_bytes() == before
    from setforge import operations

    assert operations.active("vmh") is None


def test_to_before_multi_step_revert_holds_lock_before_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``revert --to-before`` must enter profile_lock before any patch-reverse."""
    cfg, _dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)

    runner = CliRunner()
    install_result = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_result.exit_code == 0, install_result.output

    # Find the single install transition id to target with --to-before.
    from setforge import transitions

    listings = transitions.list_transitions(profile_filter=["vmh"], reverse=True)
    assert listings, "expected at least one recorded transition"
    target_id = listings[0].directory.name

    events = _install_recording_lock(monkeypatch)
    revert_result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={target_id}",
            "--yes",
        ],
    )
    assert revert_result.exit_code == 0, revert_result.output

    assert "enter" in events, "multi-step revert never acquired the profile lock"
    assert "apply" in events, "multi-step revert never performed a patch-reverse"
    assert events.index("enter") < events.index("apply"), (
        f"lock must be held before mutating; observed order: {events}"
    )


@pytest.mark.parametrize("multi_step", [False, True])
def test_revert_locks_cross_profile_transition_state_before_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_step: bool,
) -> None:
    """Migration-style state snapshots extend the revert lock envelope."""
    cfg, _dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    runner = CliRunner()
    installed = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert installed.exit_code == 0, installed.output
    latest = transitions.load_latest("vmh")
    assert latest is not None
    foreign_state = transitions.StateSnapshotEntry(
        store=transitions.SnapshotStore.BASE,
        profile="actual",
        key="file",
        payload=None,
    )
    monkeypatch.setattr(
        "setforge.cli.revert.transitions.load_state_snapshots",
        lambda _transition: (foreign_state,),
    )
    captured: list[tuple[str, ...]] = []

    @contextlib.contextmanager
    def recording_lock(**scopes: object):
        profiles = scopes["profiles"]
        assert isinstance(profiles, tuple)
        captured.append(profiles)
        yield

    monkeypatch.setattr("setforge.cli.revert.mutation_locks", recording_lock)
    monkeypatch.setattr(
        "setforge.cli.revert.operations.refuse_active",
        lambda _profile: (_ for _ in ()).throw(RuntimeError("stop before apply")),
    )
    args = ["revert", "--profile=vmh", f"--config={cfg}", "--yes"]
    if multi_step:
        args.append(f"--to-before={latest.name}")

    result = runner.invoke(app, args)

    assert isinstance(result.exception, RuntimeError)
    assert captured == [("actual", "vmh")]


def test_single_revert_refuses_transition_added_during_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer transition appearing before lock acquisition forces a retry."""
    cfg, _dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    runner = CliRunner()
    installed = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert installed.exit_code == 0, installed.output

    import setforge.cli.revert as revert_mod

    selected = revert_mod.transitions.load_latest("vmh")
    assert selected is not None
    calls = 0

    def changing_latest(_profile: str):
        nonlocal calls
        calls += 1
        return selected if calls == 1 else None

    monkeypatch.setattr(revert_mod.transitions, "load_latest", changing_latest)
    monkeypatch.setattr(
        revert_mod,
        "_apply_revert",
        lambda *_args: pytest.fail("stale confirmed transition was applied"),
    )

    result = runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"])

    assert result.exit_code != 0
    assert result.exception is not None
    assert "transition history changed after confirmation" in str(result.exception)


def test_multi_revert_refuses_chain_changed_during_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confirmed multi-step chain is re-resolved under both locks."""
    cfg, _dst = _setup_repo(tmp_path)
    _state_root(tmp_path, monkeypatch)
    _no_code(monkeypatch)
    runner = CliRunner()
    installed = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert installed.exit_code == 0, installed.output

    import setforge.cli.revert as revert_mod

    listings = revert_mod.transitions.list_transitions(
        profile_filter=["vmh"], reverse=True
    )
    assert listings
    selected = listings[0].directory.name
    real_resolve = revert_mod._resolve_to_before_chain
    chain = real_resolve("vmh", selected)
    calls = 0

    def changing_chain(_profile: str, _target: str):
        nonlocal calls
        calls += 1
        return chain if calls == 1 else []

    monkeypatch.setattr(revert_mod, "_resolve_to_before_chain", changing_chain)
    monkeypatch.setattr(
        revert_mod,
        "_apply_revert",
        lambda *_args: pytest.fail("stale confirmed chain was applied"),
    )

    result = runner.invoke(
        app,
        [
            "revert",
            "--profile=vmh",
            f"--config={cfg}",
            f"--to-before={selected}",
            "--yes",
        ],
    )

    assert result.exit_code != 0
    assert result.exception is not None
    assert "transition history changed after confirmation" in str(result.exception)
