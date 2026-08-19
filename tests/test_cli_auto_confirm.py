"""Unit tests for setforge.cli._confirm — auto-confirm --auto* confirmation wizard."""

import re
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from setforge.capture import CaptureAction, CapturePreview
from setforge.cli import app
from setforge.cli._confirm import (
    AutoDirection,
    AutoPlan,
    FileChange,
    confirm_auto_operation,
)
from setforge.errors import ConfirmRequiresInteractive
from setforge.ui.widgets import CANCEL


class _DialogRecorder:
    """Fake for ``button_bar`` (returns its value/CANCEL directly, no ``.run()``)."""

    def __init__(
        self,
        *,
        return_value: object = True,
        side_effect: type[BaseException] | None = None,
    ) -> None:
        self._return_value = return_value
        self._side_effect = side_effect
        self.call_count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> object:
        self.call_count += 1
        if self._side_effect is not None:
            raise self._side_effect()
        return self._return_value


def _patch_dialog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_value: object = True,
    side_effect: type[BaseException] | None = None,
) -> _DialogRecorder:
    recorder = _DialogRecorder(return_value=return_value, side_effect=side_effect)
    monkeypatch.setattr("setforge.cli._confirm.button_bar", recorder)
    return recorder


def _make_plan(
    *,
    direction: AutoDirection = AutoDirection.TRACKED_TO_LIVE,
    file_changes: tuple[FileChange, ...] = (
        FileChange(
            source=Path("/x/tracked.md"),
            dest=Path("/y/live.md"),
            changed=2,
        ),
    ),
    risks: tuple[str, ...] = ("live values will be overwritten",),
    revert_command: str = "setforge revert --profile=test",
) -> AutoPlan:
    return AutoPlan(
        direction=direction,
        file_changes=file_changes,
        risks=risks,
        revert_command=revert_command,
    )


# --- dataclass invariants ---


def test_autodirection_strenum_values() -> None:
    assert AutoDirection.TRACKED_TO_LIVE.value == "tracked-to-live"
    assert AutoDirection.LIVE_TO_TRACKED.value == "live-to-tracked"
    assert str(AutoDirection.TRACKED_TO_LIVE) == "tracked-to-live"


def test_filechange_slots_frozen() -> None:
    fc = FileChange(source=Path("/a"), dest=Path("/b"))
    with pytest.raises(FrozenInstanceError):
        fc.changed = 9  # type: ignore[misc]
    assert "__slots__" in dir(type(fc))


def test_autoplan_slots_frozen() -> None:
    plan = _make_plan()
    with pytest.raises(FrozenInstanceError):
        plan.revert_command = "nope"  # type: ignore[misc]
    assert "__slots__" in dir(type(plan))


def test_capture_plan_claims_only_actual_updates_and_lists_blockers() -> None:
    from setforge.cli.sync import _build_capture_plan

    ctx = SimpleNamespace(profile="p")
    plan = _build_capture_plan(
        preview=(
            CapturePreview(
                name="promoted",
                src=Path("/repo/tracked/promoted"),
                dst=Path("/live/promoted"),
                action=CaptureAction.UPDATED,
            ),
            CapturePreview(
                name="held",
                src=Path("/repo/tracked/held"),
                dst=Path("/live/held"),
                action=CaptureAction.NOOP,
                warnings=("held: changed SHARED unit requires re-confirmation",),
            ),
        ),
        ctx=ctx,  # type: ignore[arg-type]
    )
    assert [change.dest.name for change in plan.file_changes] == ["promoted"]
    assert plan.blockers == ("held: changed SHARED unit requires re-confirmation",)
    assert "1 tracked-side file(s)" in plan.risks[0]


def test_filechange_default_change_counts() -> None:
    fc = FileChange(source=Path("/a"), dest=Path("/b"))
    assert fc.changed == 0


# --- confirm_auto_operation behavior ---


def test_yes_short_circuits_true(monkeypatch: pytest.MonkeyPatch) -> None:
    dlg = _patch_dialog(monkeypatch)
    assert (
        confirm_auto_operation(
            command="install --auto=use-tracked",
            profile="test",
            plan=_make_plan(),
            yes=True,
        )
        is True
    )
    assert dlg.call_count == 0


def test_yes_short_circuit_skips_panel_rendering() -> None:
    console = Console(record=True)
    confirm_auto_operation(
        command="install --auto=use-tracked",
        profile="test",
        plan=_make_plan(),
        yes=True,
        console=console,
    )
    assert console.export_text() == ""


def test_empty_plan_skips_confirm_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = _make_plan(file_changes=(), risks=())
    dlg = _patch_dialog(monkeypatch)
    assert (
        confirm_auto_operation(
            command="install --auto=use-tracked",
            profile="test",
            plan=empty,
            yes=False,
        )
        is True
    )
    assert dlg.call_count == 0


def test_non_tty_without_yes_raises_confirm_requires_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(ConfirmRequiresInteractive) as exc:
        confirm_auto_operation(
            command="sync --auto=use-live",
            profile="test",
            plan=_make_plan(),
            yes=False,
        )
    assert "--yes" in str(exc.value)


def test_non_tty_raise_path_renders_no_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY check fires BEFORE panel rendering — non-TTY callers see
    nothing on the wizard console, only the global handler's
    ``error: ... requires --yes`` line."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    console = Console(record=True)
    with pytest.raises(ConfirmRequiresInteractive):
        confirm_auto_operation(
            command="sync --auto=use-live",
            profile="test",
            plan=_make_plan(),
            yes=False,
            console=console,
        )
    assert console.export_text() == ""


def test_tty_yes_response_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=True)
    console = Console(record=True)
    assert (
        confirm_auto_operation(
            command="install --auto=use-tracked",
            profile="test",
            plan=_make_plan(),
            yes=False,
            console=console,
        )
        is True
    )
    assert "proceeding" in console.export_text()


def test_tty_no_response_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=False)
    console = Console(record=True)
    assert (
        confirm_auto_operation(
            command="install --auto=use-tracked",
            profile="test",
            plan=_make_plan(),
            yes=False,
            console=console,
        )
        is False
    )
    assert "aborted" in console.export_text()


def test_tty_dialog_returns_cancel_treated_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=CANCEL)
    assert (
        confirm_auto_operation(
            command="install --auto=use-tracked",
            profile="test",
            plan=_make_plan(),
            yes=False,
        )
        is False
    )


def test_keyboard_interrupt_during_confirm_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        confirm_auto_operation(
            command="install --auto=use-tracked",
            profile="test",
            plan=_make_plan(),
            yes=False,
        )


# --- panel content ---


def test_panel_includes_revert_command_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=True)
    console = Console(record=True, width=120)
    plan = _make_plan(revert_command="setforge revert --profile=foo")
    confirm_auto_operation(
        command="install --auto=use-tracked",
        profile="foo",
        plan=plan,
        yes=False,
        console=console,
    )
    assert "setforge revert --profile=foo" in console.export_text()


def test_panel_includes_all_file_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=True)
    console = Console(record=True, width=200)
    plan = _make_plan(
        file_changes=tuple(
            FileChange(
                source=Path(f"/t/f{i}.md"),
                dest=Path(f"/l/f{i}.md"),
                changed=i,
            )
            for i in (1, 2, 3)
        ),
    )
    confirm_auto_operation(
        command="install --auto=use-tracked",
        profile="t",
        plan=plan,
        yes=False,
        console=console,
    )
    text = console.export_text()
    for i in (1, 2, 3):
        assert f"f{i}.md" in text


def test_panel_includes_all_risk_bullets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=True)
    console = Console(record=True, width=200)
    plan = _make_plan(risks=("risk A", "risk B", "risk C"))
    confirm_auto_operation(
        command="x",
        profile="t",
        plan=plan,
        yes=False,
        console=console,
    )
    text = console.export_text()
    for r in ("risk A", "risk B", "risk C"):
        assert r in text


def test_panel_distinguishes_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=True)
    console = Console(record=True, width=200)
    plan = _make_plan(direction=AutoDirection.LIVE_TO_TRACKED)
    confirm_auto_operation(
        command="sync --auto=use-live",
        profile="t",
        plan=plan,
        yes=False,
        console=console,
    )
    assert "live-to-tracked" in console.export_text()


# ---------------------------------------------------------------------------
# Integration via typer.testing.CliRunner
# ---------------------------------------------------------------------------


class _ConfirmRecorder:
    """Stand-in for ``confirm_auto_operation`` that records call count."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> bool:
        self.call_count += 1
        return True


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _setup_minimal_profile(tmp_path: Path) -> Path:
    """Minimal valid setforge.yaml + tracked tree for CliRunner integration."""
    yaml_path = tmp_path / "setforge.yaml"
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "x").write_text("data\n", encoding="utf-8")
    yaml_path.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: x\n"
        f"    dst: {tmp_path}/live/x\n"
        "profiles:\n"
        "  testp:\n"
        "    tracked_files: [d]\n",
        encoding="utf-8",
    )
    return yaml_path


def _write_live(yaml_path: Path, text: str) -> Path:
    live = yaml_path.parent / "live" / "x"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(text, encoding="utf-8")
    return live


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from rich-rendered help text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


@pytest.fixture
def stubbed_install_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Stub the IO-touching dependencies of install/sync integration tests.

    Returns the ``setforge.yaml`` path for ``runner.invoke`` callers.
    Shared by the four ``test_*_no_confirm`` tests that all need the
    same ``resolve_binary`` / ``ensure_state_dir_writable`` /
    ``write_transition`` stubs.
    """
    yaml_path = _setup_minimal_profile(tmp_path)
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "setforge.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )
    return yaml_path


def test_install_help_lists_yes(runner: CliRunner) -> None:
    result = runner.invoke(app, ["install", "--help"])
    assert result.exit_code == 0
    assert "--yes" in _strip_ansi(result.stdout)


def test_sync_help_lists_yes(runner: CliRunner) -> None:
    result = runner.invoke(app, ["sync", "--help"])
    assert result.exit_code == 0
    assert "--yes" in _strip_ansi(result.stdout)


def test_install_bare_no_auto_no_confirm(
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare install never invokes the confirm wizard."""
    confirm = _ConfirmRecorder()
    monkeypatch.setattr("setforge.cli._install_helpers.confirm_auto_operation", confirm)
    runner.invoke(
        app, ["install", "--profile=testp", f"--config={stubbed_install_env}"]
    )
    assert confirm.call_count == 0


def test_install_auto_keep_live_no_confirm(
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-mutating --auto=keep-live never invokes the confirm wizard."""
    confirm = _ConfirmRecorder()
    monkeypatch.setattr("setforge.cli._install_helpers.confirm_auto_operation", confirm)
    runner.invoke(
        app,
        [
            "install",
            "--profile=testp",
            f"--config={stubbed_install_env}",
            "--auto=keep-live",
        ],
    )
    assert confirm.call_count == 0


def test_sync_bare_no_auto_no_confirm(
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirm = _ConfirmRecorder()
    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    runner.invoke(app, ["sync", "--profile=testp", f"--config={stubbed_install_env}"])
    assert confirm.call_count == 0


def test_sync_auto_keep_tracked_no_confirm(
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirm = _ConfirmRecorder()
    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    runner.invoke(
        app,
        [
            "sync",
            "--profile=testp",
            f"--config={stubbed_install_env}",
            "--auto=keep-tracked",
        ],
    )
    assert confirm.call_count == 0


@pytest.mark.parametrize("command", ["capture", "sync"])
def test_capture_commands_bare_drift_non_tty_give_auto_guidance(
    command: str, runner: CliRunner, stubbed_install_env: Path
) -> None:
    _write_live(stubbed_install_env, "edited live\n")
    result = runner.invoke(
        app,
        [command, "--profile=testp", f"--config={stubbed_install_env}"],
    )
    assert result.exit_code == 1
    assert "--auto=use-live --yes" in result.output
    assert "--auto=keep-tracked" in result.output
    assert (stubbed_install_env.parent / "tracked" / "x").read_text() == "data\n"


@pytest.mark.parametrize("command", ["capture", "sync"])
def test_capture_commands_use_live_share_yes_contract(
    command: str, runner: CliRunner, stubbed_install_env: Path
) -> None:
    _write_live(stubbed_install_env, "edited live\n")
    args = [
        command,
        "--profile=testp",
        f"--config={stubbed_install_env}",
        "--auto=use-live",
    ]
    refused = runner.invoke(app, args)
    assert refused.exit_code == 1
    assert "requires --yes" in refused.output

    accepted = runner.invoke(app, [*args, "--yes"])
    assert accepted.exit_code == 0, accepted.output
    assert (stubbed_install_env.parent / "tracked" / "x").read_text() == (
        "edited live\n"
    )


@pytest.mark.parametrize("command", ["capture", "sync"])
def test_yes_without_auto_is_rejected_consistently(
    command: str,
    runner: CliRunner,
    stubbed_install_env: Path,
) -> None:
    args = [command, "--profile=testp", f"--config={stubbed_install_env}", "--yes"]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "--yes requires --auto" in result.output


@pytest.mark.parametrize("command", ["capture", "sync"])
def test_keep_tracked_is_unlocked_non_mutating_and_does_not_prompt(
    command: str,
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_live(stubbed_install_env, "edited live\n")
    confirm = _ConfirmRecorder()
    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    result = runner.invoke(
        app,
        [
            command,
            "--profile=testp",
            f"--config={stubbed_install_env}",
            "--auto=keep-tracked",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert confirm.call_count == 0
    assert (stubbed_install_env.parent / "tracked" / "x").read_text() == "data\n"
    assert "skipped" in result.output


def test_capture_prompt_runs_between_lock_acquisitions(
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    _write_live(stubbed_install_env, "edited live\n")
    depth = 0
    events: list[str] = []

    @contextlib.contextmanager
    def locks(**_kwargs: object):
        nonlocal depth
        depth += 1
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            depth -= 1

    def confirm(*_args: object, **_kwargs: object) -> bool:
        assert depth == 0
        events.append("confirm")
        return True

    monkeypatch.setattr("setforge.cli.sync.mutation_locks", locks)
    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    monkeypatch.setattr(
        "setforge.cli.sync.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True), argv=[]),
    )
    result = runner.invoke(
        app,
        ["capture", "--profile=testp", f"--config={stubbed_install_env}"],
    )
    assert result.exit_code == 0, result.output
    assert events == [
        "lock-enter",
        "lock-exit",
        "confirm",
        "lock-enter",
        "lock-exit",
    ]
    assert (stubbed_install_env.parent / "tracked" / "x").read_text() == (
        "edited live\n"
    )


def test_capture_refuses_same_count_live_substitution_after_confirmation(
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _write_live(stubbed_install_env, "edited one\n")

    def confirm(*_args: object, **_kwargs: object) -> bool:
        live.write_text("edited two\n", encoding="utf-8")
        return True

    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    monkeypatch.setattr(
        "setforge.cli.sync.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True), argv=[]),
    )
    result = runner.invoke(
        app,
        ["capture", "--profile=testp", f"--config={stubbed_install_env}"],
    )
    assert result.exit_code == 1
    assert "plan changed after confirmation" in result.output
    assert (stubbed_install_env.parent / "tracked" / "x").read_text() == "data\n"


def test_sync_refuses_config_drift_before_journal(
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_live(stubbed_install_env, "edited live\n")

    def confirm(*_args: object, **_kwargs: object) -> bool:
        with stubbed_install_env.open("a", encoding="utf-8") as stream:
            stream.write("# changed after prompt\n")
        return True

    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    monkeypatch.setattr(
        "setforge.cli.sync.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True), argv=[]),
    )
    monkeypatch.setattr(
        "setforge.cli.sync.operations.prepare",
        lambda **_kwargs: pytest.fail("journal started before plan drift refusal"),
    )
    result = runner.invoke(
        app,
        ["sync", "--profile=testp", f"--config={stubbed_install_env}"],
    )
    assert result.exit_code == 1
    assert "plan changed after confirmation" in result.output
    assert (stubbed_install_env.parent / "tracked" / "x").read_text() == "data\n"


def test_capture_prompt_ctrl_c_has_no_partial_write_warning(
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_live(stubbed_install_env, "edited live\n")

    def interrupt(*_args: object, **_kwargs: object) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", interrupt)
    monkeypatch.setattr(
        "setforge.cli.sync.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True), argv=[]),
    )
    result = runner.invoke(
        app,
        ["capture", "--profile=testp", f"--config={stubbed_install_env}"],
    )
    assert result.exit_code != 0
    assert "partially written" not in result.output
    assert (stubbed_install_env.parent / "tracked" / "x").read_text() == "data\n"


def test_capture_refuses_tracked_drift_after_confirmation(
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_live(stubbed_install_env, "edited live\n")
    tracked = stubbed_install_env.parent / "tracked" / "x"

    def confirm(*_args: object, **_kwargs: object) -> bool:
        tracked.write_text("concurrent tracked edit\n", encoding="utf-8")
        return True

    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    monkeypatch.setattr(
        "setforge.cli.sync.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True), argv=[]),
    )
    result = runner.invoke(
        app,
        ["capture", "--profile=testp", f"--config={stubbed_install_env}"],
    )
    assert result.exit_code == 1
    assert "plan changed after confirmation" in result.output
    assert tracked.read_text() == "concurrent tracked edit\n"


@pytest.mark.parametrize("drift_leg", ["base", "index", "drafts"])
def test_capture_refuses_staged_store_drift_after_confirmation(
    drift_leg: str,
    runner: CliRunner,
    stubbed_install_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from setforge import locking
    from setforge.reconcile import hunks, store
    from setforge.reconcile.types import HunkClass, content_sha, file_id

    base = b"data\n"
    live_bytes = b"edited live\n"
    draft = b"portable\n"
    _write_live(stubbed_install_env, live_bytes.decode())
    fid = file_id("d")
    (fresh,) = hunks.extract_hunks(base, live_bytes)
    drafted = replace(
        fresh,
        cls=HunkClass.SHARED_DRAFTED,
        draft_hash=content_sha(draft),
    )
    with locking.profile_lock("testp"):
        store.record(
            "testp",
            fid,
            base=base,
            local=live_bytes,
            staged=True,
            hunks=hunks.serialize([drafted]),
            drafts={drafted.ref: draft},
        )

    def confirm(*_args: object, **_kwargs: object) -> bool:
        with locking.profile_lock("testp"):
            if drift_leg == "base":
                store.write_base("testp", fid, b"concurrent base\n")
            elif drift_leg == "index":
                store.record(
                    "testp",
                    fid,
                    base=base,
                    local=live_bytes,
                    staged=True,
                    hunks=hunks.serialize(
                        [replace(drafted, cls=HunkClass.LOCAL, draft_hash=None)]
                    ),
                    drafts={},
                )
            else:
                store.write_drafts("testp", fid, {drafted.ref: b"changed draft\n"})
        return True

    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    monkeypatch.setattr(
        "setforge.cli.sync.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True), argv=[]),
    )
    result = runner.invoke(
        app,
        ["capture", "--profile=testp", f"--config={stubbed_install_env}"],
    )
    assert result.exit_code == 1
    assert "plan changed after confirmation" in result.output
    assert (stubbed_install_env.parent / "tracked" / "x").read_bytes() == base
