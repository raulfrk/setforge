"""Unit tests for setforge.cli._confirm — bviv --auto* confirmation wizard."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from setforge.cli import app
from setforge.cli._confirm import (
    AutoDirection,
    AutoPlan,
    ConfirmRequiresInteractive,
    FileChange,
    confirm_auto_operation,
)


class _FakeDialogResult:
    """Stand-in for prompt_toolkit's ``Dialog`` return object.

    The real ``radiolist_dialog(...)`` returns a ``Dialog`` whose
    ``.run()`` yields the user's choice. Tests configure
    ``.run()`` to return a value, raise ``KeyboardInterrupt``, etc.
    """

    def __init__(
        self,
        *,
        return_value: object = True,
        side_effect: type[BaseException] | None = None,
    ) -> None:
        self._return_value = return_value
        self._side_effect = side_effect
        self.run_calls = 0

    def run(self) -> object:
        self.run_calls += 1
        if self._side_effect is not None:
            raise self._side_effect()
        return self._return_value


class _DialogRecorder:
    """Callable that records invocation and returns a configured fake.

    Replaces ``setforge.cli._confirm.radiolist_dialog`` so tests can
    assert the dialog was/was-not invoked without
    ``unittest.mock.MagicMock`` semantics.
    """

    def __init__(self, fake: _FakeDialogResult | None = None) -> None:
        self.fake = fake or _FakeDialogResult()
        self.call_count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeDialogResult:
        self.call_count += 1
        return self.fake


def _patch_dialog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_value: object = True,
    side_effect: type[BaseException] | None = None,
) -> _DialogRecorder:
    """Replace ``radiolist_dialog`` with a recorder; return it for assertions."""
    recorder = _DialogRecorder(
        _FakeDialogResult(return_value=return_value, side_effect=side_effect)
    )
    monkeypatch.setattr("setforge.cli._confirm.radiolist_dialog", recorder)
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


def test_tty_dialog_returns_none_treated_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User pressing Esc returns None from radiolist_dialog → abort."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=None)
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


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from rich-rendered help text."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_install_help_lists_yes(runner: CliRunner) -> None:
    result = runner.invoke(app, ["install", "--help"])
    assert result.exit_code == 0
    assert "--yes" in _strip_ansi(result.stdout)


def test_sync_help_lists_yes(runner: CliRunner) -> None:
    result = runner.invoke(app, ["sync", "--help"])
    assert result.exit_code == 0
    assert "--yes" in _strip_ansi(result.stdout)


def test_install_bare_no_auto_no_confirm(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare install never invokes the confirm wizard."""
    yaml_path = _setup_minimal_profile(tmp_path)
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "setforge.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )
    confirm = _ConfirmRecorder()
    monkeypatch.setattr(
        "setforge.cli._install_helpers.confirm_auto_operation", confirm
    )
    runner.invoke(app, ["install", "--profile=testp", f"--config={yaml_path}"])
    assert confirm.call_count == 0


def test_install_auto_keep_live_no_confirm(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-mutating --auto=keep-live never invokes the confirm wizard."""
    yaml_path = _setup_minimal_profile(tmp_path)
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "setforge.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )
    confirm = _ConfirmRecorder()
    monkeypatch.setattr(
        "setforge.cli._install_helpers.confirm_auto_operation", confirm
    )
    runner.invoke(
        app,
        [
            "install",
            "--profile=testp",
            f"--config={yaml_path}",
            "--auto=keep-live",
        ],
    )
    assert confirm.call_count == 0


def test_sync_bare_no_auto_no_confirm(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _setup_minimal_profile(tmp_path)
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "setforge.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )
    confirm = _ConfirmRecorder()
    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    runner.invoke(app, ["sync", "--profile=testp", f"--config={yaml_path}"])
    assert confirm.call_count == 0


def test_sync_auto_keep_tracked_no_confirm(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _setup_minimal_profile(tmp_path)
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "setforge.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )
    confirm = _ConfirmRecorder()
    monkeypatch.setattr("setforge.cli.sync.confirm_auto_operation", confirm)
    runner.invoke(
        app,
        [
            "sync",
            "--profile=testp",
            f"--config={yaml_path}",
            "--auto=keep-tracked",
        ],
    )
    assert confirm.call_count == 0
