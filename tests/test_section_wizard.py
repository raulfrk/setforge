"""Tests for the install-side section reconcile wizard."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from my_setup.section_reconcile import SectionDrift, SectionDriftState
from my_setup.section_wizard import (
    ReconcileAuto,
    SectionAction,
    format_drift_summary,
    reconcile_sections,
)
from my_setup.sections import SectionSemantics


def _drift(
    name: str,
    semantics: SectionSemantics,
    state: SectionDriftState,
    tracked_body: str,
    live_body: str,
) -> SectionDrift:
    return SectionDrift(
        name=name,
        semantics=semantics,
        state=state,
        tracked_body=tracked_body,
        live_body=live_body,
    )


# ---------------------------------------------------------------------------
# auto modes
# ---------------------------------------------------------------------------


def test_reconcile_use_tracked_overwrites_shared_drift() -> None:
    drifts = {
        "workflow": _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "new tracked\n",
            "old live\n",
        )
    }
    out = reconcile_sections(drifts, auto=ReconcileAuto.USE_TRACKED, interactive=False)
    assert out["workflow"].body == "new tracked\n"
    assert out["workflow"].action is SectionAction.TAKE_TRACKED


def test_reconcile_keep_live_keeps_live_body() -> None:
    drifts = {
        "workflow": _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "new tracked\n",
            "old live\n",
        )
    }
    out = reconcile_sections(drifts, auto=ReconcileAuto.KEEP_LIVE, interactive=False)
    assert out["workflow"].body == "old live\n"
    assert out["workflow"].action is SectionAction.KEEP_LIVE


def test_reconcile_use_tracked_overwrites_even_under_conflict() -> None:
    """USE_TRACKED bypasses three-way state — forceful."""
    drifts = {
        "workflow": _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.CONFLICT,
            "tracked v2\n",
            "live v2\n",
        )
    }
    out = reconcile_sections(drifts, auto=ReconcileAuto.USE_TRACKED, interactive=False)
    assert out["workflow"].body == "tracked v2\n"


def test_reconcile_host_local_always_keeps_live() -> None:
    drifts = {
        "notes": _drift(
            "notes",
            SectionSemantics.HOST_LOCAL,
            SectionDriftState.LIVE_EDITED,
            "tracked\n",
            "live edits\n",
        )
    }
    out = reconcile_sections(drifts, auto=ReconcileAuto.USE_TRACKED, interactive=False)
    assert out["notes"].body == "live edits\n"
    assert out["notes"].action is SectionAction.KEEP_LIVE


def test_reconcile_no_drift_keeps_live_silently() -> None:
    drifts = {
        "workflow": _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.NO_DRIFT,
            "same\n",
            "same\n",
        )
    }
    out = reconcile_sections(drifts, auto=None, interactive=False)
    assert out["workflow"].body == "same\n"
    assert out["workflow"].action is SectionAction.KEEP_LIVE


def test_reconcile_bare_install_no_auto_keeps_live() -> None:
    """Default (no flag, no auto, no interactive) = keep-live silently."""
    drifts = {
        "workflow": _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "tracked\n",
            "live\n",
        )
    }
    out = reconcile_sections(drifts, auto=None, interactive=False)
    assert out["workflow"].body == "live\n"
    assert out["workflow"].action is SectionAction.KEEP_LIVE


def test_reconcile_iteration_order_preserved() -> None:
    drifts = {
        "first": _drift(
            "first",
            SectionSemantics.SHARED,
            SectionDriftState.NO_DRIFT,
            "x\n",
            "x\n",
        ),
        "second": _drift(
            "second",
            SectionSemantics.SHARED,
            SectionDriftState.NO_DRIFT,
            "y\n",
            "y\n",
        ),
    }
    out = reconcile_sections(drifts, auto=ReconcileAuto.KEEP_LIVE, interactive=False)
    assert list(out.keys()) == ["first", "second"]


# ---------------------------------------------------------------------------
# Interactive prompt (piped stdin, single keypress)
# ---------------------------------------------------------------------------


class _StdinPipe:
    """Replace sys.stdin with a StringIO that lacks fileno().

    Triggers the non-tty fallback in
    :func:`my_setup.wizard.read_one_choice`.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        self._monkeypatch = monkeypatch
        self._buf = io.StringIO(text)

    def __enter__(self) -> _StdinPipe:
        self._monkeypatch.setattr(sys, "stdin", self._buf)
        return self

    def __exit__(self, *a: object) -> None:
        # monkeypatch handles undo
        pass


def test_reconcile_interactive_keep_live_via_keypress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifts = {
        "workflow": _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "new tracked\n",
            "old live\n",
        )
    }
    with _StdinPipe(monkeypatch, "k\n"):
        out = reconcile_sections(drifts, auto=None, interactive=True)
    assert out["workflow"].action is SectionAction.KEEP_LIVE
    assert out["workflow"].body == "old live\n"


def test_reconcile_interactive_take_tracked_via_keypress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifts = {
        "workflow": _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "new tracked\n",
            "old live\n",
        )
    }
    with _StdinPipe(monkeypatch, "t\n"):
        out = reconcile_sections(drifts, auto=None, interactive=True)
    assert out["workflow"].action is SectionAction.TAKE_TRACKED
    assert out["workflow"].body == "new tracked\n"


def test_reconcile_interactive_skip_then_keep_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two drifted shared sections: skip first, keep live on the second."""
    drifts = {
        "workflow": _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "t1\n",
            "l1\n",
        ),
        "commits": _drift(
            "commits",
            SectionSemantics.SHARED,
            SectionDriftState.LIVE_EDITED,
            "t2\n",
            "l2\n",
        ),
    }
    with _StdinPipe(monkeypatch, "sk"):
        out = reconcile_sections(drifts, auto=None, interactive=True)
    assert out["workflow"].action is SectionAction.SKIP
    assert out["workflow"].body == "l1\n"
    assert out["commits"].action is SectionAction.KEEP_LIVE
    assert out["commits"].body == "l2\n"


def test_reconcile_interactive_quit_keeps_rest_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After 'q' the wizard keeps-live every remaining section without
    asking — single key press → all sections resolved."""
    drifts = {
        "a": _drift(
            "a",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "ta\n",
            "la\n",
        ),
        "b": _drift(
            "b",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "tb\n",
            "lb\n",
        ),
        "c": _drift(
            "c",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "tc\n",
            "lc\n",
        ),
    }
    with _StdinPipe(monkeypatch, "q"):
        out = reconcile_sections(drifts, auto=None, interactive=True)
    assert out["a"].action is SectionAction.QUIT_KEEP_REST
    assert out["b"].action is SectionAction.KEEP_LIVE
    assert out["c"].action is SectionAction.KEEP_LIVE
    assert all(d.body.startswith("l") for d in out.values())


def test_reconcile_interactive_edit_action_runs_editor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """'e' opens $EDITOR seeded with live body; the wizard returns
    whatever the editor wrote back."""
    # Fake $EDITOR with a tiny shell script that overwrites the file.
    fake_editor = tmp_path / "fake_editor.sh"
    fake_editor.write_text(
        '#!/usr/bin/env bash\nprintf "edited body\\n" > "$1"\n', encoding="utf-8"
    )
    os.chmod(fake_editor, 0o755)
    monkeypatch.setenv("EDITOR", str(fake_editor))

    drifts = {
        "workflow": _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "tracked\n",
            "live seed\n",
        )
    }
    with _StdinPipe(monkeypatch, "e"):
        out = reconcile_sections(drifts, auto=None, interactive=True)
    assert out["workflow"].action is SectionAction.EDIT
    assert out["workflow"].body == "edited body\n"


# ---------------------------------------------------------------------------
# Summary formatter
# ---------------------------------------------------------------------------


def test_format_drift_summary_empty_when_no_shared_drift() -> None:
    drifts = [
        _drift(
            "a",
            SectionSemantics.HOST_LOCAL,
            SectionDriftState.LIVE_EDITED,
            "t\n",
            "l\n",
        )
    ]
    assert format_drift_summary(drifts) == ""


def test_format_drift_summary_skips_no_drift_shared() -> None:
    drifts = [
        _drift("a", SectionSemantics.SHARED, SectionDriftState.NO_DRIFT, "x\n", "x\n")
    ]
    assert format_drift_summary(drifts) == ""


def test_format_drift_summary_pending_tracked_singular() -> None:
    drifts = [
        _drift(
            "workflow",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "t\n",
            "l\n",
        )
    ]
    summary = format_drift_summary(drifts)
    assert "1 shared section drifted" in summary
    assert "pending tracked update" in summary


def test_format_drift_summary_multiple_categories_plural() -> None:
    drifts = [
        _drift(
            "a",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "t\n",
            "l\n",
        ),
        _drift(
            "b",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "t\n",
            "l\n",
        ),
        _drift(
            "c",
            SectionSemantics.SHARED,
            SectionDriftState.LIVE_EDITED,
            "t\n",
            "l\n",
        ),
        _drift(
            "d",
            SectionSemantics.SHARED,
            SectionDriftState.CONFLICT,
            "t\n",
            "l\n",
        ),
    ]
    summary = format_drift_summary(drifts)
    assert "4 shared sections drifted" in summary
    assert "2 pending tracked updates" in summary
    assert "1 live edit" in summary
    assert "1 three-way conflict" in summary


def test_format_drift_summary_includes_legacy_and_inconsistent() -> None:
    drifts = [
        _drift("a", SectionSemantics.SHARED, SectionDriftState.LEGACY, "t\n", "l\n"),
        _drift(
            "b",
            SectionSemantics.SHARED,
            SectionDriftState.INCONSISTENT,
            "t\n",
            "l\n",
        ),
    ]
    summary = format_drift_summary(drifts)
    assert "legacy" in summary
    assert "inconsistent" in summary


def test_format_drift_summary_iterates_enum_order() -> None:
    """Summary fragments appear in ``SectionDriftState`` declaration order.

    Locks in the new iteration contract: deleting
    ``_DRIFT_SUMMARY_STATES`` made enum declaration order the de facto
    summary order, so a future enum reorder would silently change
    user-visible warning text. This test makes that order load-bearing.
    """
    drifts = [
        _drift(
            "i",
            SectionSemantics.SHARED,
            SectionDriftState.INCONSISTENT,
            "t\n",
            "l\n",
        ),
        _drift("c", SectionSemantics.SHARED, SectionDriftState.CONFLICT, "t\n", "l\n"),
        _drift("le", SectionSemantics.SHARED, SectionDriftState.LEGACY, "t\n", "l\n"),
        _drift(
            "li",
            SectionSemantics.SHARED,
            SectionDriftState.LIVE_EDITED,
            "t\n",
            "l\n",
        ),
        _drift(
            "p",
            SectionSemantics.SHARED,
            SectionDriftState.PENDING_TRACKED,
            "t\n",
            "l\n",
        ),
    ]
    summary = format_drift_summary(drifts)
    # Order must match SectionDriftState declaration order:
    # NO_DRIFT (skipped), LEGACY, PENDING_TRACKED, LIVE_EDITED, CONFLICT,
    # INCONSISTENT.
    fragments_section = summary.split(": ", 1)[1]
    legacy_idx = fragments_section.index("1 legacy (no embedded hash)")
    pending_idx = fragments_section.index("1 pending tracked update")
    live_idx = fragments_section.index("1 live edit")
    conflict_idx = fragments_section.index("1 three-way conflict")
    inconsistent_idx = fragments_section.index("1 inconsistent")
    assert legacy_idx < pending_idx < live_idx < conflict_idx < inconsistent_idx
