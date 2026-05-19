"""Unit tests for setforge.cli._revert_confirm — p1vl revert wizard."""

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


class _FakeDialogResult:
    """Stand-in for prompt_toolkit's ``Dialog`` return object."""

    def __init__(
        self,
        *,
        return_value: object = RevertChoice.ABORT,
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
    """Callable that records invocation and returns a configured fake."""

    def __init__(self, fake: _FakeDialogResult | None = None) -> None:
        self.fake = fake or _FakeDialogResult()
        self.call_count = 0
        self.last_kwargs: dict[str, Any] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeDialogResult:
        self.call_count += 1
        self.last_kwargs = kwargs
        return self.fake


def _patch_dialog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_value: object = RevertChoice.ABORT,
    side_effect: type[BaseException] | None = None,
) -> _DialogRecorder:
    recorder = _DialogRecorder(
        _FakeDialogResult(return_value=return_value, side_effect=side_effect)
    )
    monkeypatch.setattr("setforge.cli._revert_confirm.radiolist_dialog", recorder)
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
    assert RevertChoice.APPLY_WITH_EDITOR.value == "apply-with-editor"
    assert str(RevertChoice.APPLY) == "apply"


def test_file_mutation_frozen_slots() -> None:
    fm = FileMutation(path=Path("/x"), diff_summary="+1 -0")
    with pytest.raises(FrozenInstanceError):
        fm.diff_summary = "nope"  # type: ignore[misc]
    assert "__slots__" in dir(type(fm))


def test_file_mutation_default_user_edit_collision() -> None:
    fm = FileMutation(path=Path("/x"), diff_summary="+1 -0")
    assert fm.user_edit_collision == ()


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


def test_tty_dialog_apply_with_editor_returns_apply_with_editor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY_WITH_EDITOR)
    choice = confirm_revert_operation(plan=_make_plan(), yes=False)
    assert choice is RevertChoice.APPLY_WITH_EDITOR


def test_tty_dialog_returns_none_treated_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User pressing Esc returns None from radiolist_dialog → ABORT."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=None)
    choice = confirm_revert_operation(plan=_make_plan(), yes=False)
    assert choice is RevertChoice.ABORT


def test_tty_dialog_returns_false_treated_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: prompt_toolkit can return False on cancel in some
    versions; treat as ABORT same as None."""
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
    """Safe-default invariant per acceptance #5: default selected option
    in the dialog is ABORT."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    recorder = _patch_dialog(monkeypatch, return_value=RevertChoice.ABORT)
    confirm_revert_operation(plan=_make_plan(), yes=False)
    assert recorder.last_kwargs.get("default") is RevertChoice.ABORT


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


def test_panel_calls_out_user_edit_collision_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_value=RevertChoice.APPLY)
    console = Console(record=True, width=200)
    plan = _make_plan(
        file_mutations=(
            FileMutation(
                path=Path("/x/CLAUDE.md"),
                diff_summary="+14 -3",
                user_edit_collision=((14, 22), (47, 49)),
            ),
        ),
    )
    confirm_revert_operation(plan=plan, yes=False, console=console)
    text = console.export_text()
    # Collisions surface in RISKS panel — mockup line 44-48.
    assert "14-22" in text or "14" in text
    assert "47-49" in text or "47" in text


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


# ---------------------------------------------------------------------------
# CLI integration via CliRunner (revert.py --yes wiring)
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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
    to apply_patch_reverse + _write_reverse_transition."""
    import shutil

    if shutil.which("patch") is None:
        pytest.skip("GNU patch not on PATH")

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
    recorder = _DialogRecorder(_FakeDialogResult(return_value=RevertChoice.APPLY))
    monkeypatch.setattr("setforge.cli._revert_confirm.radiolist_dialog", recorder)

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
    import shutil

    if shutil.which("patch") is None:
        pytest.skip("GNU patch not on PATH")

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


def test_revert_apply_with_editor_opens_editor_then_reprompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """APPLY_WITH_EDITOR opens the editor, then re-prompts. Second prompt
    returning APPLY must apply the revert."""
    import shutil

    if shutil.which("patch") is None:
        pytest.skip("GNU patch not on PATH")

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

    # First confirm() → APPLY_WITH_EDITOR; second confirm() → APPLY.
    return_sequence = [RevertChoice.APPLY_WITH_EDITOR, RevertChoice.APPLY]
    call_count = {"n": 0}

    def fake_confirm(*, plan: Any, yes: bool, console: Any = None) -> RevertChoice:
        idx = call_count["n"]
        call_count["n"] += 1
        return return_sequence[idx]

    monkeypatch.setattr("setforge.cli.revert.confirm_revert_operation", fake_confirm)

    editor_calls: list[Path] = []

    def fake_editor(target: Path) -> None:
        editor_calls.append(target)

    monkeypatch.setattr("setforge.cli.revert.run_editor", fake_editor)

    install_res = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_res.exit_code == 0
    assert dst.exists()

    revert_res = runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}"])
    assert revert_res.exit_code == 0, revert_res.output
    assert not dst.exists()
    assert call_count["n"] == 2
    assert len(editor_calls) == 1


def test_revert_apply_with_editor_then_abort_leaves_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """APPLY_WITH_EDITOR → editor → ABORT on re-prompt must leave files."""
    import shutil

    if shutil.which("patch") is None:
        pytest.skip("GNU patch not on PATH")

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

    return_sequence = [RevertChoice.APPLY_WITH_EDITOR, RevertChoice.ABORT]
    call_count = {"n": 0}

    def fake_confirm(*, plan: Any, yes: bool, console: Any = None) -> RevertChoice:
        idx = call_count["n"]
        call_count["n"] += 1
        return return_sequence[idx]

    monkeypatch.setattr("setforge.cli.revert.confirm_revert_operation", fake_confirm)
    monkeypatch.setattr("setforge.cli.revert.run_editor", lambda _: None)

    install_res = runner.invoke(app, ["install", "--profile=vmh", f"--config={cfg}"])
    assert install_res.exit_code == 0
    assert dst.exists()

    revert_res = runner.invoke(app, ["revert", "--profile=vmh", f"--config={cfg}"])
    assert revert_res.exit_code == 0
    assert dst.exists(), "ABORT after editor must leave files untouched"
    assert call_count["n"] == 2
