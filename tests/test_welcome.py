"""Unit tests for setforge.cli._welcome — fresh-host welcome panel.

Every test in this module opts INTO fresh-host conditions via the
``fresh_host`` pytest marker, which suppresses the tests/conftest.py
autouse fixture that plants a transition record. The fixture-suppression
inversion keeps every OTHER install test in the suite unaffected by the
welcome gate (the welcome would otherwise fire on every CliRunner
``install`` invocation under the sandboxed-HOME autouse).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from setforge.cli import app
from setforge.cli._helpers import ProfileContext
from setforge.cli._welcome import (
    OverlayDelta,
    WelcomeChoice,
    WelcomeInventory,
    build_welcome_inventory,
    is_fresh_host,
    prompt_welcome,
    reject_auto_on_fresh_host,
)
from setforge.config import load_config, resolve_profile
from setforge.errors import WelcomeRequiresInteractive

pytestmark = pytest.mark.fresh_host

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e"
_FIXTURE_YAML = _FIXTURE_DIR / "setforge.test.yaml"
_FIXTURE_TRACKED = _FIXTURE_DIR / "tracked"


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """Copy the e2e fixture into ``tmp_path``; return path to the test yaml."""
    target = tmp_path / "repo"
    target.mkdir()
    shutil.copy2(_FIXTURE_YAML, target / "setforge.test.yaml")
    shutil.copytree(_FIXTURE_TRACKED, target / "tracked")
    return target / "setforge.test.yaml"


@pytest.fixture
def sandboxed_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``$HOME`` + ``SETFORGE_STATE_DIR`` to ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    return home


@pytest.fixture
def no_external_bins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``code`` and ``claude`` resolution so reconcilers no-op."""
    monkeypatch.setattr(
        "setforge.vscode_extensions.resolve_binary", lambda _name: None
    )
    from setforge import claude_plugins as cp

    cp._get_claude_bin.cache_clear()
    monkeypatch.setattr("setforge.claude_plugins.resolve_binary", lambda _name: None)


def _build_ctx(fixture_repo: Path, profile: str = "test-minimal") -> ProfileContext:
    cfg = load_config(fixture_repo)
    return ProfileContext(
        cfg=cfg,
        resolved=resolve_profile(cfg, profile),
        repo_root=fixture_repo.resolve().parent,
        profile=profile,
    )


class _FakeDialogResult:
    """Stand-in for prompt_toolkit's Dialog return object."""

    def __init__(self, return_value: object = WelcomeChoice.PROCEED) -> None:
        self._return_value = return_value
        self.run_calls = 0

    def run(self) -> object:
        self.run_calls += 1
        return self._return_value


class _DialogRecorder:
    """Records radiolist_dialog invocations; returns a configured fake."""

    def __init__(self, *returns: object) -> None:
        # ``returns`` is consumed in order — each call to the dialog
        # pulls the next configured return value. Useful for the
        # dry-run-first reprompt case where one test exercises two
        # dialog invocations with different responses.
        self._returns = list(returns) or [WelcomeChoice.PROCEED]
        self.call_count = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> _FakeDialogResult:
        idx = min(self.call_count, len(self._returns) - 1)
        value = self._returns[idx]
        self.call_count += 1
        return _FakeDialogResult(return_value=value)


def _patch_dialog(
    monkeypatch: pytest.MonkeyPatch, *returns: object
) -> _DialogRecorder:
    """Replace ``radiolist_dialog`` with a recorder; return it for assertions."""
    recorder = _DialogRecorder(*returns)
    monkeypatch.setattr("setforge.cli._welcome.radiolist_dialog", recorder)
    return recorder


# ---------------------------------------------------------------------------
# is_fresh_host
# ---------------------------------------------------------------------------


def test_is_fresh_host_no_transitions(sandboxed_home: Path) -> None:
    """No transitions dir on disk → fresh host."""
    assert is_fresh_host() is True


def test_is_fresh_host_empty_transitions_dir(
    sandboxed_home: Path, tmp_path: Path
) -> None:
    """Transitions dir exists but is empty → fresh host."""
    (tmp_path / "state" / "transitions").mkdir(parents=True)
    assert is_fresh_host() is True


def test_is_fresh_host_claude_dir_alone(sandboxed_home: Path) -> None:
    """``~/.claude/`` exists (VSCode created it) but no transitions → fresh.

    Anchors anti-pattern check 3: detection MUST NOT bind to
    ``~/.claude/`` existence.
    """
    (sandboxed_home / ".claude").mkdir()
    (sandboxed_home / ".claude" / "CLAUDE.md").write_text("noise", encoding="utf-8")
    assert is_fresh_host() is True


def test_is_fresh_host_returns_false_when_meta_present(
    sandboxed_home: Path, tmp_path: Path
) -> None:
    """Transition record present → not fresh."""
    txn = tmp_path / "state" / "transitions" / "20260519T120000000000Z-install-x"
    txn.mkdir(parents=True)
    (txn / "meta.json").write_text(json.dumps({"command": "install"}), encoding="utf-8")
    assert is_fresh_host() is False


def test_is_fresh_host_ignores_non_dir_children(
    sandboxed_home: Path, tmp_path: Path
) -> None:
    """Stray files in transitions root are ignored, only meta.json dirs count."""
    root = tmp_path / "state" / "transitions"
    root.mkdir(parents=True)
    (root / "stray.txt").write_text("debris", encoding="utf-8")
    assert is_fresh_host() is True


# ---------------------------------------------------------------------------
# WelcomeInventory
# ---------------------------------------------------------------------------


def test_build_welcome_inventory_counts(
    fixture_repo: Path, sandboxed_home: Path
) -> None:
    """Inventory captures tracked / plugin / extension / bootstrap counts."""
    ctx = _build_ctx(fixture_repo, profile="test-comprehensive")
    inv = build_welcome_inventory(ctx)
    assert isinstance(inv, WelcomeInventory)
    assert inv.profile == "test-comprehensive"
    assert inv.tracked_file_count > 0
    # Comprehensive fixture declares plugins + extensions + bootstrap.
    assert inv.plugin_count == len(ctx.resolved.claude_plugins)
    assert inv.extension_count == len(ctx.resolved.extensions.include)
    assert inv.bootstrap_count == len(ctx.resolved.bootstrap)


def test_welcome_inventory_is_frozen() -> None:
    inv = WelcomeInventory(
        tracked_file_count=0,
        dst_dirs_to_create=(),
        plugin_count=0,
        extension_count=0,
        bootstrap_count=0,
        overlay_delta=OverlayDelta(),
        profile="x",
    )
    with pytest.raises(AttributeError):
        inv.profile = "y"  # type: ignore[misc]


def test_build_welcome_inventory_overlay_delta_is_zero(
    fixture_repo: Path, sandboxed_home: Path
) -> None:
    """Fresh inventory carries a zero-shaped :class:`OverlayDelta`.

    Spec B (local.yaml ``preserve_user_keys`` overlay) is not yet
    implemented, so every channel reads zero. When spec B lands, this
    test grows fixtures that drive non-zero values; the contract that
    :class:`OverlayDelta` is always present on the inventory stays.
    """
    ctx = _build_ctx(fixture_repo, profile="test-comprehensive")
    inv = build_welcome_inventory(ctx)
    assert inv.overlay_delta == OverlayDelta()
    assert inv.overlay_delta.is_empty


# ---------------------------------------------------------------------------
# prompt_welcome behavior
# ---------------------------------------------------------------------------


def _empty_inventory(
    profile: str = "test",
    overlay_delta: OverlayDelta | None = None,
) -> WelcomeInventory:
    return WelcomeInventory(
        tracked_file_count=3,
        dst_dirs_to_create=(Path("/tmp/x"),),
        plugin_count=1,
        extension_count=2,
        bootstrap_count=1,
        overlay_delta=overlay_delta or OverlayDelta(),
        profile=profile,
    )


def test_yes_flag_skips_welcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """``yes=True`` short-circuits to PROCEED, no dialog rendered."""
    dlg = _patch_dialog(monkeypatch)
    choice = prompt_welcome(inventory=_empty_inventory(), yes=True)
    assert choice is WelcomeChoice.PROCEED
    assert dlg.call_count == 0


def test_yes_flag_skips_panel_rendering() -> None:
    """``yes=True`` skips even the panel render — no console output."""
    console = Console(record=True)
    prompt_welcome(inventory=_empty_inventory(), yes=True, console=console)
    assert console.export_text() == ""


def test_non_tty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-TTY + no ``--yes`` → WelcomeRequiresInteractive."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(WelcomeRequiresInteractive) as exc:
        prompt_welcome(inventory=_empty_inventory(), yes=False)
    assert "--yes" in str(exc.value)


def test_non_tty_renders_no_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTY check fires BEFORE panel render — no console output on raise."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    console = Console(record=True)
    with pytest.raises(WelcomeRequiresInteractive):
        prompt_welcome(inventory=_empty_inventory(), yes=False, console=console)
    assert console.export_text() == ""


def test_panel_renders_overlay_row_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh-host panel renders the 6th category row with zero overlay deltas."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, WelcomeChoice.PROCEED)
    console = Console(record=True, width=120)
    prompt_welcome(inventory=_empty_inventory(), yes=False, console=console)
    text = console.export_text()
    assert "applied local.yaml overlay" in text
    # Zero-shaped delta renders as "0p+/0p-/0x+/0x-/0s".
    assert "0p+/0p-/0x+/0x-/0s" in text


def test_panel_renders_overlay_row_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero :class:`OverlayDelta` surfaces every channel in the 6th row.

    Documents the row format so spec B (local.yaml overlay schema) lands
    with a known-shape integration point: the welcome surface MUST
    render every overlay channel without re-spec'ing the panel.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, WelcomeChoice.PROCEED)
    console = Console(record=True, width=120)
    delta = OverlayDelta(
        plugin_add=2,
        plugin_remove=1,
        extension_add=3,
        extension_remove=0,
        host_local_sections=4,
    )
    prompt_welcome(
        inventory=_empty_inventory(overlay_delta=delta),
        yes=False,
        console=console,
    )
    text = console.export_text()
    assert "2p+/1p-/3x+/0x-/4s" in text


def test_tty_proceed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, WelcomeChoice.PROCEED)
    console = Console(record=True)
    choice = prompt_welcome(
        inventory=_empty_inventory(), yes=False, console=console
    )
    assert choice is WelcomeChoice.PROCEED
    text = console.export_text()
    assert "fresh-host detected" in text
    assert "tracked file" in text


def test_tty_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """ABORT returns ABORT and prints the abort marker; caller exits 0."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, WelcomeChoice.ABORT)
    console = Console(record=True)
    choice = prompt_welcome(
        inventory=_empty_inventory(), yes=False, console=console
    )
    assert choice is WelcomeChoice.ABORT
    assert "aborted" in console.export_text()


def test_tty_abort_show_docs_prints_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, WelcomeChoice.ABORT_SHOW_DOCS)
    console = Console(record=True)
    choice = prompt_welcome(
        inventory=_empty_inventory(), yes=False, console=console
    )
    assert choice is WelcomeChoice.ABORT_SHOW_DOCS
    text = console.export_text()
    assert "aborted" in text
    assert "docs/INSTALL.md" in text


def test_tty_esc_falls_back_to_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Esc on the dialog (``None`` return) → ABORT."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, None)
    console = Console(record=True)
    choice = prompt_welcome(
        inventory=_empty_inventory(), yes=False, console=console
    )
    assert choice is WelcomeChoice.ABORT


def test_dry_run_first_reprompt_no_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRY_RUN_FIRST invokes the callback once, then reprompts WITHOUT
    the DRY_RUN_FIRST option (no infinite recursion possible)."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # First dialog: DRY_RUN_FIRST. Second dialog (reprompt): PROCEED.
    dlg = _patch_dialog(monkeypatch, WelcomeChoice.DRY_RUN_FIRST, WelcomeChoice.PROCEED)
    dry_run_calls: list[int] = []

    def fake_dry_run() -> None:
        dry_run_calls.append(1)

    choice = prompt_welcome(
        inventory=_empty_inventory(),
        yes=False,
        run_dry_run=fake_dry_run,
    )
    assert choice is WelcomeChoice.PROCEED
    assert len(dry_run_calls) == 1
    # Exactly two dialog invocations: initial + reprompt.
    assert dlg.call_count == 2


def test_dry_run_first_then_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reprompt path correctly handles ABORT after dry-run."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, WelcomeChoice.DRY_RUN_FIRST, WelcomeChoice.ABORT)
    console = Console(record=True)
    dry_run_calls: list[int] = []
    choice = prompt_welcome(
        inventory=_empty_inventory(),
        yes=False,
        run_dry_run=lambda: dry_run_calls.append(1),
        console=console,
    )
    assert choice is WelcomeChoice.ABORT
    assert len(dry_run_calls) == 1
    assert "aborted" in console.export_text()


def test_dry_run_first_without_callback_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRY_RUN_FIRST chosen but no callback → RuntimeError (caller bug)."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, WelcomeChoice.DRY_RUN_FIRST)
    with pytest.raises(RuntimeError, match="run_dry_run"):
        prompt_welcome(inventory=_empty_inventory(), yes=False, run_dry_run=None)


# ---------------------------------------------------------------------------
# reject_auto_on_fresh_host
# ---------------------------------------------------------------------------


def test_reject_auto_none_is_noop() -> None:
    """``auto=None`` means no flag passed → noop."""
    reject_auto_on_fresh_host(auto=None)


def test_reject_auto_use_tracked_raises_exit_2() -> None:
    import typer as _typer

    with pytest.raises(_typer.Exit) as exc:
        reject_auto_on_fresh_host(auto="use-tracked")
    assert exc.value.exit_code == 2


def test_reject_auto_keep_live_raises_exit_2() -> None:
    import typer as _typer

    with pytest.raises(_typer.Exit) as exc:
        reject_auto_on_fresh_host(auto="keep-live")
    assert exc.value.exit_code == 2


# ---------------------------------------------------------------------------
# Integration with the install CLI command
# ---------------------------------------------------------------------------


def _invoke_install(
    fixture_repo: Path,
    *,
    profile: str = "test-minimal",
    extra: list[str] | None = None,
    input_text: str | None = None,
) -> object:
    args = [
        "install",
        f"--profile={profile}",
        f"--config={fixture_repo}",
        "--no-git-check",
        "--no-transition",
    ]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args, input=input_text)


def test_install_non_tty_no_yes_raises_welcome_requires_interactive(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
) -> None:
    """Fresh host + non-TTY + no --yes → WelcomeRequiresInteractive
    surfaces through the install CLI exit handler.

    ``CliRunner.invoke`` does NOT route through ``setforge.cli.main``'s
    ``SetforgeError`` handler — it lets the exception bubble onto
    ``result.exception``. The non-TTY assertion is exercised at the
    boundary: a TTY-less ``CliRunner`` invocation MUST surface the
    exception type on the result.
    """
    result = _invoke_install(fixture_repo, input_text="")
    assert result.exit_code != 0
    assert isinstance(result.exception, WelcomeRequiresInteractive)
    assert "--yes" in str(result.exception)


def test_install_yes_skips_welcome(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--yes`` on a fresh host skips the welcome and continues to install."""
    # Tripwire: dialog must NOT be invoked when --yes is set.
    dlg = _patch_dialog(monkeypatch)
    result = _invoke_install(fixture_repo, extra=["--yes"])
    assert result.exit_code == 0, result.output
    assert dlg.call_count == 0


def test_install_dry_run_skips_welcome(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` skips the welcome (dry-run is itself the preview)."""
    dlg = _patch_dialog(monkeypatch)
    result = _invoke_install(fixture_repo, extra=["--dry-run"])
    assert result.exit_code == 0, result.output
    assert dlg.call_count == 0


def test_install_auto_on_fresh_host_rejected(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
) -> None:
    """``--auto=*`` on a fresh host exits 2 with the expected message."""
    result = _invoke_install(fixture_repo, extra=["--auto=use-tracked"])
    assert result.exit_code == 2
    assert "no drift exists on fresh host" in (result.output or "")


def test_install_after_first_run_skips_welcome(
    fixture_repo: Path,
    sandboxed_home: Path,
    no_external_bins: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Once a transition record exists, the welcome is suppressed."""
    # Plant a transition record so is_fresh_host returns False.
    txn = tmp_path / "state" / "transitions" / "20260519T100000000000Z-install-x"
    txn.mkdir(parents=True)
    (txn / "meta.json").write_text(json.dumps({"command": "install"}), encoding="utf-8")
    dlg = _patch_dialog(monkeypatch)
    # No --yes; the welcome should NOT fire because the host is not fresh.
    result = _invoke_install(fixture_repo)
    assert result.exit_code == 0, result.output
    assert dlg.call_count == 0
