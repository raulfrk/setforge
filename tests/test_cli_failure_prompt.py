"""Unit tests for the setforge-k0uj reconcile failure-action wizard.

Exercises :class:`setforge.cli._confirm.FailureAction` plus
:func:`setforge.cli._confirm.prompt_failure_action` — the arrow-key
picker that fronts per-item reconcile failures with skip / retry /
abort / diagnose options.
"""

from __future__ import annotations

from typing import Any

import pytest
from rich.console import Console

from setforge.cli._confirm import (
    FailureAction,
    prompt_failure_action,
)
from setforge.cli._plugin_helpers import _emit_reconcile_summary
from setforge.errors import ConfirmRequiresInteractive
from setforge.transitions import ReconcileKind, ReconcileOutcome, ReconcileStatus


class _FakeDialogResult:
    """Stand-in for ``prompt_toolkit.shortcuts.radiolist_dialog``'s return.

    ``.run()`` consumes one entry from ``return_values`` per call so a
    DIAGNOSE re-prompt can resolve to a different terminal action than
    the first invocation.
    """

    def __init__(self, return_values: list[Any]) -> None:
        self._queue = list(return_values)
        self.run_calls = 0

    def run(self) -> Any:
        self.run_calls += 1
        if not self._queue:
            raise AssertionError("ran out of fake dialog responses")
        return self._queue.pop(0)


class _DialogRecorder:
    """Records each ``radiolist_dialog(...)`` invocation for assertions."""

    def __init__(self, return_values: list[Any]) -> None:
        self.fake = _FakeDialogResult(return_values)
        self.call_count = 0
        self.last_kwargs: dict[str, Any] = {}

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeDialogResult:
        self.call_count += 1
        self.last_kwargs = kwargs
        return self.fake


def _patch_dialog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    return_values: list[Any],
) -> _DialogRecorder:
    """Replace ``radiolist_dialog`` with a recorder; return it for assertions."""
    recorder = _DialogRecorder(return_values)
    monkeypatch.setattr("setforge.cli._confirm.radiolist_dialog", recorder)
    return recorder


# --- FailureAction enum invariants ----------------------------------------


def test_failure_action_strenum_values() -> None:
    assert FailureAction.SKIP.value == "skip"
    assert FailureAction.RETRY.value == "retry"
    assert FailureAction.ABORT.value == "abort"
    assert FailureAction.DIAGNOSE.value == "diagnose"


def test_failure_action_string_equality() -> None:
    """StrEnum members compare equal to their string values — used by
    the install command boundary's status filter (``status == 'skipped'``)
    and acceptance command (i)."""
    assert FailureAction.SKIP == "skip"
    assert str(FailureAction.SKIP) == "skip"


# --- yes=True short-circuit ----------------------------------------------


def test_yes_short_circuits_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """``yes=True`` returns the default without consulting the dialog
    — preserves the historic warn-and-continue posture for scripted
    contexts."""
    dlg = _patch_dialog(monkeypatch, return_values=[])
    assert (
        prompt_failure_action(
            message="failed: foo@bar\nsubprocess exit 1",
            yes=True,
        )
        is FailureAction.SKIP
    )
    assert dlg.call_count == 0


def test_yes_short_circuits_to_custom_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """``default=...`` parameter shapes the ``yes=True`` short-circuit."""
    _patch_dialog(monkeypatch, return_values=[])
    assert (
        prompt_failure_action(
            message="failed",
            default=FailureAction.ABORT,
            yes=True,
        )
        is FailureAction.ABORT
    )


# --- non-TTY behavior ----------------------------------------------------


def test_non_tty_without_yes_raises_confirm_requires_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror :func:`confirm_auto_operation`'s non-interactive gate: no
    TTY + no ``--yes`` raises so the global handler surfaces a clean
    ``error: ... requires --yes`` line."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(ConfirmRequiresInteractive) as exc:
        prompt_failure_action(message="failed", yes=False)
    assert "--yes" in str(exc.value)


# --- TTY + arrow-key picker ----------------------------------------------


def test_tty_skip_response_returns_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_values=[FailureAction.SKIP])
    console = Console(record=True)
    result = prompt_failure_action(message="failed: x", yes=False, console=console)
    assert result is FailureAction.SKIP
    assert "reconcile failure" in console.export_text()


def test_tty_retry_response_returns_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_values=[FailureAction.RETRY])
    assert prompt_failure_action(message="failed: x", yes=False) is FailureAction.RETRY


def test_tty_abort_response_returns_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_values=[FailureAction.ABORT])
    assert prompt_failure_action(message="failed: x", yes=False) is FailureAction.ABORT


# --- Esc / None handling -------------------------------------------------


def test_dialog_returns_none_treated_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User pressing Esc returns None from radiolist_dialog → ABORT.

    Consistent with :func:`confirm_auto_operation`'s Esc-as-abort
    handling. Critical for the failure-prompt path: an accidental Esc
    on a network-flaky mid-reconcile MUST NOT silently skip — it must
    surface as ABORT so the user knows the install is rolling back."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_values=[None])
    assert prompt_failure_action(message="failed: x", yes=False) is FailureAction.ABORT


# --- DIAGNOSE re-prompt loop ---------------------------------------------


def test_diagnose_re_prompts_and_prints_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DIAGNOSE is the only non-terminal choice: it prints the captured
    stderr trace and re-prompts. The function never returns DIAGNOSE
    — it loops until the user picks SKIP / RETRY / ABORT."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    dlg = _patch_dialog(
        monkeypatch,
        return_values=[FailureAction.DIAGNOSE, FailureAction.SKIP],
    )
    console = Console(record=True)
    result = prompt_failure_action(
        message="failed: foo",
        full_stderr="full subprocess stderr here\nline 2",
        yes=False,
        console=console,
    )
    assert result is FailureAction.SKIP
    assert dlg.call_count == 2  # initial + re-prompt after DIAGNOSE
    text = console.export_text()
    assert "failure trace" in text
    assert "full subprocess stderr" in text


def test_diagnose_without_full_stderr_prints_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the caller has no captured trace, DIAGNOSE still prints a
    placeholder so the user gets a consistent feedback loop."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(
        monkeypatch,
        return_values=[FailureAction.DIAGNOSE, FailureAction.RETRY],
    )
    console = Console(record=True)
    prompt_failure_action(
        message="failed", full_stderr=None, yes=False, console=console
    )
    assert "no captured trace" in console.export_text()


def test_diagnose_then_abort_returns_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-prompt after DIAGNOSE can land on ABORT — verifies the
    loop terminates cleanly on every terminal choice, not just SKIP."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(
        monkeypatch,
        return_values=[FailureAction.DIAGNOSE, FailureAction.ABORT],
    )
    assert prompt_failure_action(message="failed", yes=False) is FailureAction.ABORT


def test_diagnose_then_none_returns_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-prompt after DIAGNOSE also honors Esc-as-abort — the
    full Esc/None handling is in the loop body, not gated by first
    iteration."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(
        monkeypatch,
        return_values=[FailureAction.DIAGNOSE, None],
    )
    assert prompt_failure_action(message="failed", yes=False) is FailureAction.ABORT


# --- prompt message content ----------------------------------------------


def test_prompt_includes_failure_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_dialog(monkeypatch, return_values=[FailureAction.SKIP])
    console = Console(record=True, width=200)
    prompt_failure_action(
        message="failed: secure-code-review@work-internal\nfetch timed out (30s)",
        yes=False,
        console=console,
    )
    text = console.export_text()
    assert "secure-code-review@work-internal" in text
    assert "fetch timed out" in text


# --- reconcile summary tally (acceptance #6) -----------------------------


def test_emit_reconcile_summary_renders_all_status_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_emit_reconcile_summary`` tallies per-kind status counts and
    emits the ``summary:`` block per spec acceptance #6 / mockup E.

    Mixed-status outcome tuples must render:
    - ``N plugins reconciled (M required retry, K skipped: <ids>)``
    - ``N extensions reconciled (M required retry, K skipped: <ids>)``

    ``N`` = ``ok`` + ``retried_ok`` (items that landed); ``M`` =
    ``retried_ok`` only; ``K`` = ``skipped``. ``aborted`` outcomes are
    excluded from the totals — they represent rollback bookkeeping for
    items the user explicitly abandoned, not items reconciled. The
    ``K skipped`` parenthetical lists ids comma-separated so the user
    can spot which item to re-target with ``--retry-failed``.
    """
    plugin_outcomes: tuple[ReconcileOutcome, ...] = (
        ReconcileOutcome(
            item_id="alpha@official",
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.OK,
            error_summary=None,
        ),
        ReconcileOutcome(
            item_id="beta@official",
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.OK,
            error_summary=None,
        ),
        ReconcileOutcome(
            item_id="gamma@work",
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.RETRIED_OK,
            error_summary=None,
        ),
        ReconcileOutcome(
            item_id="delta@work",
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.SKIPPED,
            error_summary="fetch failed",
        ),
    )
    ext_outcomes: tuple[ReconcileOutcome, ...] = (
        ReconcileOutcome(
            item_id="charliermarsh.ruff",
            kind=ReconcileKind.EXTENSION,
            status=ReconcileStatus.OK,
            error_summary=None,
        ),
        ReconcileOutcome(
            item_id="esbenp.prettier-vscode",
            kind=ReconcileKind.EXTENSION,
            status=ReconcileStatus.OK,
            error_summary=None,
        ),
        ReconcileOutcome(
            item_id="work-only-extension",
            kind=ReconcileKind.EXTENSION,
            status=ReconcileStatus.SKIPPED,
            error_summary="not found in registry",
        ),
    )
    _emit_reconcile_summary(plugin_outcomes, ext_outcomes)
    out = capsys.readouterr().out
    assert "summary:" in out
    # Plugin line: 3 reconciled (2 ok + 1 retried_ok), 1 retry, 1 skipped.
    assert "3 plugins reconciled" in out
    assert "1 required retry" in out
    assert "1 skipped: delta@work" in out
    # Extension line: 2 reconciled, 0 retries, 1 skipped.
    assert "2 extensions reconciled" in out
    assert "1 skipped: work-only-extension" in out


def test_emit_reconcile_summary_omits_zero_only_kind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty outcome tuples skip the corresponding ``N <kind>s reconciled``
    line entirely — a profile with no extensions shouldn't render a
    misleading ``0 extensions reconciled`` row, and a profile with no
    plugin work shouldn't render a misleading ``0 plugins reconciled``
    row. Mirrors the spec mockup's no-pad-with-zeros shape."""
    plugin_outcomes: tuple[ReconcileOutcome, ...] = (
        ReconcileOutcome(
            item_id="alpha@official",
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.OK,
            error_summary=None,
        ),
    )
    _emit_reconcile_summary(plugin_outcomes, ())
    out = capsys.readouterr().out
    assert "summary:" in out
    assert "1 plugins reconciled" in out
    assert "extensions reconciled" not in out


def test_emit_reconcile_summary_all_clean_omits_parenthetical(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When no retries or skips occurred, the per-kind line drops the
    parenthetical entirely — keeps the summary compact for the
    happy-path install."""
    plugin_outcomes: tuple[ReconcileOutcome, ...] = (
        ReconcileOutcome(
            item_id="alpha@official",
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.OK,
            error_summary=None,
        ),
        ReconcileOutcome(
            item_id="beta@official",
            kind=ReconcileKind.PLUGIN,
            status=ReconcileStatus.OK,
            error_summary=None,
        ),
    )
    _emit_reconcile_summary(plugin_outcomes, ())
    out = capsys.readouterr().out
    assert "2 plugins reconciled" in out
    assert "(" not in out  # no parenthetical when no retry / no skip


def test_emit_reconcile_summary_empty_outcomes_emits_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two empty tuples → no output at all. An install that reconciled
    nothing (e.g. tracked-files-only profile, or `claude` / `code`
    binaries missing) should not surface a bare ``summary:`` header."""
    _emit_reconcile_summary((), ())
    out = capsys.readouterr().out
    assert out == ""
