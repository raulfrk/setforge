"""Unit tests for setforge.cli._revert_confirm — revert-confirm revert wizard."""

import re
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from setforge.cli import app
from setforge.cli._revert_confirm import (
    ExtensionOperation,
    ExtensionReconcile,
    FileMutation,
    PluginOperation,
    PluginReconcile,
    RevertChoice,
    RevertPlan,
    confirm_revert_operation,
)
from setforge.errors import ConfirmRequiresInteractive

# ---------------------------------------------------------------------------
# Fakes — mirror the shape from tests/test_cli_auto_confirm.py
# ---------------------------------------------------------------------------


class _DialogRecorder:
    def __init__(
        self,
        *,
        return_value: object = RevertChoice.ABORT,
        side_effect: type[BaseException] | None = None,
    ) -> None:
        self._return_value = return_value
        self._side_effect = side_effect
        self.call_count = 0
        self.last_args: tuple[Any, ...] = ()
        self.last_kwargs: dict[str, Any] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> object:
        self.call_count += 1
        self.last_args = args
        self.last_kwargs = kwargs
        if self._side_effect is not None:
            raise self._side_effect()
        return self._return_value


def _patch_dialog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_value: object = RevertChoice.ABORT,
    side_effect: type[BaseException] | None = None,
) -> _DialogRecorder:
    recorder = _DialogRecorder(return_value=return_value, side_effect=side_effect)
    monkeypatch.setattr("setforge.cli._revert_confirm.button_bar", recorder)
    return recorder


def _make_plan(
    *,
    transition_id: str = "20260518T201433-install-vm-headless",
    transition_type: str = "install",
    profile: str = "vm-headless",
    age_human: str = "11 minutes ago",
    file_mutations: tuple[FileMutation, ...] = (
        FileMutation(
            path=Path("/home/u/.claude/CLAUDE.md"),
            diff_summary="+14 -3",
        ),
    ),
    plugin_reconciles: tuple[PluginReconcile, ...] = (),
    extension_reconciles: tuple[ExtensionReconcile, ...] = (),
    redo_command: str = "setforge revert --profile=vm-headless",
) -> RevertPlan:
    return RevertPlan(
        transition_id=transition_id,
        transition_type=transition_type,
        profile=profile,
        age_human=age_human,
        file_mutations=file_mutations,
        plugin_reconciles=plugin_reconciles,
        extension_reconciles=extension_reconciles,
        redo_command=redo_command,
    )


# ---------------------------------------------------------------------------
# Dataclass + enum invariants
# ---------------------------------------------------------------------------


def test_revert_choice_strenum_values() -> None:
    assert RevertChoice.ABORT.value == "abort"
    assert RevertChoice.APPLY.value == "apply"
    assert str(RevertChoice.APPLY) == "apply"


def test_file_mutation_frozen_slots() -> None:
    fm = FileMutation(path=Path("/x"), diff_summary="+1 -0")
    with pytest.raises(FrozenInstanceError):
        fm.diff_summary = "nope"  # type: ignore[misc]
    assert "__slots__" in dir(type(fm))


def test_plugin_reconcile_frozen_slots() -> None:
    pr = PluginReconcile(
        plugin_id="p@m", operation=PluginOperation.ENABLED, source="[local]"
    )
    with pytest.raises(FrozenInstanceError):
        pr.plugin_id = "x"  # type: ignore[misc]
    assert "__slots__" in dir(type(pr))


def test_extension_reconcile_frozen_slots() -> None:
    er = ExtensionReconcile(
        extension_id="ext",
        operation=ExtensionOperation.INSTALLED,
        source="[profile]",
    )
    with pytest.raises(FrozenInstanceError):
        er.extension_id = "x"  # type: ignore[misc]
    assert "__slots__" in dir(type(er))


def test_revert_plan_frozen_slots() -> None:
    plan = _make_plan()
    with pytest.raises(FrozenInstanceError):
        plan.profile = "other"  # type: ignore[misc]
    assert "__slots__" in dir(type(plan))


# ---------------------------------------------------------------------------
# confirm_revert_operation: control flow
# ---------------------------------------------------------------------------


def test_yes_short_circuits_to_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    dlg = _patch_dialog(monkeypatch)
    assert confirm_revert_operation(plan=_make_plan(), yes=True) is RevertChoice.APPLY
    assert dlg.call_count == 0


def test_yes_short_circuit_renders_no_panel() -> None:
    console = Console(record=True)
    confirm_revert_operation(plan=_make_plan(), yes=True, console=console)
    assert console.export_text() == ""


def test_non_tty_without_yes_raises_confirm_requires_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(ConfirmRequiresInteractive) as exc:
        confirm_revert_operation(plan=_make_plan(), yes=False)
    assert "--yes" in str(exc.value)


def test_non_tty_raise_path_renders_no_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY check fires BEFORE panel rendering — non-TTY callers see
    nothing on the wizard console."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    console = Console(record=True)
    with pytest.raises(ConfirmRequiresInteractive):
        confirm_revert_operation(plan=_make_plan(), yes=False, console=console)
    assert console.export_text() == ""


def test_tty_dialog_apply_returns_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True)
    choice = confirm_revert_operation(plan=_make_plan(), yes=False, console=console)
    assert choice is RevertChoice.APPLY


def test_tty_dialog_abort_returns_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.ABORT)
    console = Console(record=True)
    choice = confirm_revert_operation(plan=_make_plan(), yes=False, console=console)
    assert choice is RevertChoice.ABORT
    assert "aborted" in console.export_text()


def test_tty_dialog_returns_cancel_treated_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.ui.widgets import CANCEL

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=CANCEL)
    choice = confirm_revert_operation(plan=_make_plan(), yes=False)
    assert choice is RevertChoice.ABORT


def test_tty_dialog_returns_false_treated_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=False)
    choice = confirm_revert_operation(plan=_make_plan(), yes=False)
    assert choice is RevertChoice.ABORT


def test_keyboard_interrupt_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, side_effect=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        confirm_revert_operation(plan=_make_plan(), yes=False)


def test_default_dialog_value_is_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    # Safe-default invariant per acceptance #5: initial=0 is the ABORT button.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    recorder = _patch_dialog(monkeypatch, return_value=RevertChoice.ABORT)
    confirm_revert_operation(plan=_make_plan(), yes=False)
    assert recorder.last_kwargs.get("initial") == 0
    buttons = recorder.last_args[0]
    assert buttons[0].value is RevertChoice.ABORT


def test_dialog_offers_only_abort_then_revert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    recorder = _patch_dialog(monkeypatch, return_value=RevertChoice.ABORT)
    confirm_revert_operation(plan=_make_plan(), yes=False)
    buttons = recorder.last_args[0]
    assert [(b.label, b.value) for b in buttons] == [
        ("no, abort (default — safe)", RevertChoice.ABORT),
        ("yes, revert", RevertChoice.APPLY),
    ]


# ---------------------------------------------------------------------------
# Panel content (mockup A invariants)
# ---------------------------------------------------------------------------


def test_panel_includes_transition_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True, width=160)
    plan = _make_plan(transition_id="20260518T201433-install-vm-headless")
    confirm_revert_operation(plan=plan, yes=False, console=console)
    text = console.export_text()
    assert "20260518T201433-install-vm-headless" in text


def test_panel_includes_file_count_and_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True, width=200)
    plan = _make_plan(
        file_mutations=tuple(
            FileMutation(path=Path(f"/x/f{i}.md"), diff_summary=f"+{i} -0")
            for i in (1, 2, 3)
        )
    )
    confirm_revert_operation(plan=plan, yes=False, console=console)
    text = console.export_text()
    assert "files affected (3)" in text
    for i in (1, 2, 3):
        assert f"f{i}.md" in text


def test_panel_includes_diff_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True, width=200)
    plan = _make_plan(
        file_mutations=(FileMutation(path=Path("/x/a.md"), diff_summary="+14 -3"),),
    )
    confirm_revert_operation(plan=plan, yes=False, console=console)
    text = console.export_text()
    assert "+14 -3" in text


def test_panel_includes_plugin_reconciles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True, width=200)
    plan = _make_plan(
        plugin_reconciles=(
            PluginReconcile(
                plugin_id="secure-code-review@work-internal",
                operation=PluginOperation.ENABLED,
                source="[from local.yaml]",
            ),
            PluginReconcile(
                plugin_id="some-default-plugin",
                operation=PluginOperation.DISABLED,
                source="[from profile]",
            ),
        ),
    )
    confirm_revert_operation(plan=plan, yes=False, console=console)
    text = console.export_text()
    assert "plugins reconciled (2)" in text
    assert "secure-code-review@work-internal" in text
    assert "some-default-plugin" in text


def test_panel_includes_extension_reconciles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True, width=200)
    plan = _make_plan(
        extension_reconciles=(
            ExtensionReconcile(
                extension_id="work-only-extension",
                operation=ExtensionOperation.INSTALLED,
                source="[from profile]",
            ),
        ),
    )
    confirm_revert_operation(plan=plan, yes=False, console=console)
    text = console.export_text()
    assert "extensions reconciled (1)" in text
    assert "work-only-extension" in text


def test_panel_includes_risks_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True, width=200)
    confirm_revert_operation(plan=_make_plan(), yes=False, console=console)
    assert "RISKS" in console.export_text()


def test_panel_includes_redo_command_before_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True, width=200)
    plan = _make_plan(redo_command="setforge revert --profile=vm-headless")
    confirm_revert_operation(plan=plan, yes=False, console=console)
    text = console.export_text()
    assert "REDO" in text
    assert "setforge revert --profile=vm-headless" in text


def test_panel_shows_transition_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True, width=200)
    plan = _make_plan(age_human="42 minutes ago")
    confirm_revert_operation(plan=plan, yes=False, console=console)
    assert "42 minutes ago" in console.export_text()


def test_multi_step_dialog_returns_cancel_treated_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.cli._revert_confirm import (
        MultiStepRevertPlan,
        confirm_multi_step_revert_operation,
    )
    from setforge.ui.widgets import CANCEL

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=CANCEL)
    plan = MultiStepRevertPlan(profile="vm-headless", steps=(_make_plan(),))
    console = Console(record=True)
    choice = confirm_multi_step_revert_operation(plan=plan, yes=False, console=console)
    assert choice is RevertChoice.ABORT
    assert "aborted" in console.export_text()


# ---------------------------------------------------------------------------
# CLI integration via CliRunner (revert.py --yes wiring)
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_revert_help_lists_yes(runner: CliRunner) -> None:
    result = runner.invoke(app, ["revert", "--help"])
    assert result.exit_code == 0
    assert "--yes" in _strip_ansi(result.stdout)


def test_revert_yes_short_circuits_no_dialog_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """With --yes the wizard is not rendered and revert proceeds straight
    to the file restore + _write_reverse_transition."""

    # Minimal repo + state.
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "greeting.md").write_text("hello\n", encoding="utf-8")
    dst = tmp_path / "live" / "greeting.md"
    cfg = repo / "setforge.yaml"
    cfg.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  greeting:\n"
        "    src: greeting.md\n"
        f"    dst: {dst}\n"
        "profiles:\n"
        "  vmh:\n"
        "    tracked_files: [greeting]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    recorder = _DialogRecorder(return_value=RevertChoice.APPLY)
    monkeypatch.setattr("setforge.cli._revert_confirm.button_bar", recorder)

    install_res = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_res.exit_code == 0, install_res.output
    assert dst.exists()

    revert_res = runner.invoke(
        app, ["revert", "--profile=vmh", f"--config={cfg}", "--yes"]
    )
    assert revert_res.exit_code == 0, revert_res.output
    assert not dst.exists()
    assert recorder.call_count == 0


def test_revert_abort_leaves_files_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """When the wizard returns ABORT, revert exits 0 with no mutations."""

    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "greeting.md").write_text("hello\n", encoding="utf-8")
    dst = tmp_path / "live" / "greeting.md"
    cfg = repo / "setforge.yaml"
    cfg.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  greeting:\n"
        "    src: greeting.md\n"
        f"    dst: {dst}\n"
        "profiles:\n"
        "  vmh:\n"
        "    tracked_files: [greeting]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)

    # CliRunner's stdin is not a TTY, so we stub confirm_revert_operation
    # directly at the revert.py call site (mirrors the
    # confirm_auto_operation stub pattern in tests/test_cli_auto_confirm.py).
    confirm_calls: list[Any] = []

    def fake_confirm(*, plan: Any, yes: bool, console: Any = None) -> RevertChoice:
        confirm_calls.append(plan)
        return RevertChoice.ABORT

    monkeypatch.setattr("setforge.cli.revert.confirm_revert_operation", fake_confirm)

    install_res = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_res.exit_code == 0, install_res.output
    assert dst.exists()

    revert_res = runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}"])
    assert revert_res.exit_code == 0, revert_res.output
    assert dst.exists(), "ABORT must leave files untouched"
    assert len(confirm_calls) == 1


def test_revert_interactive_apply_restores_after_one_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """Without --yes, an APPLY answer reverts after a single confirm."""

    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "greeting.md").write_text("hello\n", encoding="utf-8")
    dst = tmp_path / "live" / "greeting.md"
    cfg = repo / "setforge.yaml"
    cfg.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  greeting:\n"
        "    src: greeting.md\n"
        f"    dst: {dst}\n"
        "profiles:\n"
        "  vmh:\n"
        "    tracked_files: [greeting]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)

    confirm_calls: list[Any] = []

    def fake_confirm(*, plan: Any, yes: bool, console: Any = None) -> RevertChoice:
        confirm_calls.append(plan)
        return RevertChoice.APPLY

    monkeypatch.setattr("setforge.cli.revert.confirm_revert_operation", fake_confirm)

    install_res = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_res.exit_code == 0, install_res.output
    assert dst.exists()

    revert_res = runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}"])
    assert revert_res.exit_code == 0, revert_res.output
    assert not dst.exists()
    assert len(confirm_calls) == 1
