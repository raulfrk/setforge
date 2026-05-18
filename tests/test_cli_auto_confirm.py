"""Unit tests for setforge.cli._confirm — bviv --auto* confirmation wizard."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from setforge.cli._confirm import (
    AutoDirection,
    AutoPlan,
    ConfirmRequiresInteractive,
    FileChange,
    confirm_auto_operation,
)


def _make_plan(
    *,
    direction: AutoDirection = AutoDirection.TRACKED_TO_LIVE,
    file_changes: tuple[FileChange, ...] = (
        FileChange(
            source=Path("/x/tracked.md"),
            dest=Path("/y/live.md"),
            added=1,
            removed=0,
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
        fc.added = 9  # type: ignore[misc]
    assert "__slots__" in dir(type(fc))


def test_autoplan_slots_frozen() -> None:
    plan = _make_plan()
    with pytest.raises(FrozenInstanceError):
        plan.revert_command = "nope"  # type: ignore[misc]
    assert "__slots__" in dir(type(plan))


def test_filechange_default_change_counts() -> None:
    fc = FileChange(source=Path("/a"), dest=Path("/b"))
    assert fc.added == 0
    assert fc.removed == 0
    assert fc.changed == 0


# --- confirm_auto_operation behavior ---


def test_yes_short_circuits_true() -> None:
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        assert (
            confirm_auto_operation(
                command="install --auto=use-tracked",
                profile="test",
                plan=_make_plan(),
                yes=True,
            )
            is True
        )
        dlg.assert_not_called()


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


def test_empty_plan_skips_confirm_returns_true() -> None:
    empty = _make_plan(file_changes=(), risks=())
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        assert (
            confirm_auto_operation(
                command="install --auto=use-tracked",
                profile="test",
                plan=empty,
                yes=False,
            )
            is True
        )
        dlg.assert_not_called()


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


def test_tty_yes_response_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        dlg.return_value.run.return_value = True
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
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        dlg.return_value.run.return_value = False
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
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        dlg.return_value.run.return_value = None
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
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        dlg.return_value.run.side_effect = KeyboardInterrupt
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
    console = Console(record=True, width=120)
    plan = _make_plan(revert_command="setforge revert --profile=foo")
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        dlg.return_value.run.return_value = True
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
    console = Console(record=True, width=200)
    plan = _make_plan(
        file_changes=tuple(
            FileChange(
                source=Path(f"/t/f{i}.md"),
                dest=Path(f"/l/f{i}.md"),
                added=i,
                removed=0,
                changed=0,
            )
            for i in (1, 2, 3)
        ),
    )
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        dlg.return_value.run.return_value = True
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
    console = Console(record=True, width=200)
    plan = _make_plan(risks=("risk A", "risk B", "risk C"))
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        dlg.return_value.run.return_value = True
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
    console = Console(record=True, width=200)
    plan = _make_plan(direction=AutoDirection.LIVE_TO_TRACKED)
    with patch("setforge.cli._confirm.radiolist_dialog") as dlg:
        dlg.return_value.run.return_value = True
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

from typer.testing import CliRunner  # noqa: E402

from setforge.cli import app  # noqa: E402


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
    with patch("setforge.cli.install.confirm_auto_operation") as confirm:
        runner.invoke(app, ["install", "--profile=testp", f"--config={yaml_path}"])
        confirm.assert_not_called()


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
    with patch("setforge.cli.install.confirm_auto_operation") as confirm:
        runner.invoke(
            app,
            [
                "install",
                "--profile=testp",
                f"--config={yaml_path}",
                "--auto=keep-live",
            ],
        )
        confirm.assert_not_called()


def test_sync_bare_no_auto_no_confirm(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _setup_minimal_profile(tmp_path)
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "setforge.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )
    with patch("setforge.cli.sync.confirm_auto_operation") as confirm:
        runner.invoke(app, ["sync", "--profile=testp", f"--config={yaml_path}"])
        confirm.assert_not_called()


def test_sync_auto_keep_tracked_no_confirm(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = _setup_minimal_profile(tmp_path)
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "setforge.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )
    with patch("setforge.cli.sync.confirm_auto_operation") as confirm:
        runner.invoke(
            app,
            [
                "sync",
                "--profile=testp",
                f"--config={yaml_path}",
                "--auto=keep-tracked",
            ],
        )
        confirm.assert_not_called()
