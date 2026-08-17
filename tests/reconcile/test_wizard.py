"""Interactive per-region conflict wizard (A3).

Driven headlessly via ``create_app_session`` + ``create_pipe_input`` (the
``tests/test_ui_widgets.py`` pattern). Each region's :func:`button_bar` is
selected by its derived accelerator letter — ``o`` Ours, ``t`` Theirs, ``e``
Edit, ``c`` Claude-merge, ``s`` Skip — so a multi-region run is driven by one
byte string. ``\\x1b`` (Esc) aborts the focused region menu.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from subprocess import CalledProcessError

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from setforge.errors import SetforgeError
from setforge.reconcile import WizardResult, resolve_conflicts
from setforge.reconcile.merge_model import Clean, Conflict, MergeResult, Segment
from setforge.reconcile.types import FileId
from setforge.reconcile.wizard import ClaudeMergeFn
from setforge.ui.widgets import CANCEL


def test_reconcile_facade_rejects_unknown_lazy_export() -> None:
    import setforge.reconcile as reconcile

    with pytest.raises(AttributeError, match="has no attribute"):
        getattr(  # noqa: B009 - exercises PEP 562 fallback
            reconcile, "missing_reconcile_export"
        )


# Private helpers/consts live on the module; the façade re-exports the public API.
W = importlib.import_module("setforge.reconcile.wizard")

_FID = FileId("claude/CLAUDE.md")


def _run(
    result: MergeResult,
    keys: bytes,
    *,
    claude_merge: ClaudeMergeFn | None = None,
) -> object:
    """Drive :func:`resolve_conflicts` with ``keys`` piped to its button bars."""
    with create_pipe_input() as pipe:
        pipe.send_bytes(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            if claude_merge is None:
                return resolve_conflicts(_FID, result)
            return resolve_conflicts(_FID, result, claude_merge=claude_merge)


def _one(ours: bytes = b"A\n", theirs: bytes = b"B\n") -> MergeResult:
    """A single-conflict result."""
    return MergeResult((Conflict(base=b"", ours=ours, theirs=theirs),))


# --------------------------------------------------------------------------- #
# Precondition
# --------------------------------------------------------------------------- #


def test_clean_result_raises() -> None:
    clean = MergeResult((Clean(b"x\n"),))
    with pytest.raises(ValueError, match="conflict-free"):
        resolve_conflicts(_FID, clean)


# --------------------------------------------------------------------------- #
# Button paths — Ours / Theirs / Skip fold exact bytes
# --------------------------------------------------------------------------- #


def test_ours_folds_exact_bytes() -> None:
    out = _run(_one(b"A\n", b"B\n"), b"o")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"A\n"
    assert out.merged.clean
    assert out.deferred is False


def test_theirs_folds_exact_bytes() -> None:
    out = _run(_one(b"A\n", b"B\n"), b"t")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"B\n"
    assert out.deferred is False


def test_skip_keeps_ours_and_marks_deferred() -> None:
    out = _run(_one(b"A\n", b"B\n"), b"s")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"A\n"
    assert out.deferred is True


def test_multi_region_interleaves_clean_runs() -> None:
    result = MergeResult(
        (
            Conflict(base=b"", ours=b"A\n", theirs=b"B\n"),
            Clean(b"middle\n"),
            Conflict(base=b"", ours=b"C\n", theirs=b"D\n"),
        )
    )
    out = _run(result, b"to")  # region 1 -> Theirs, region 2 -> Ours
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"B\nmiddle\nC\n"


def test_clean_segments_pass_through_identically() -> None:
    clean = Clean(b"keep me\n")
    result = MergeResult((clean, Conflict(base=b"", ours=b"A\n", theirs=b"B\n")))
    out = _run(result, b"o")
    assert isinstance(out, WizardResult)
    # INV-6: the original Clean object is reused verbatim, not rebuilt.
    assert out.merged.segments[0] is clean


def test_wizard_markers_do_not_leak_into_merged() -> None:
    # The git markers the wizard draws/seeds are display-only; Ours/Theirs/Skip
    # fold raw side bytes, so no marker may survive into the resolved file.
    result = MergeResult(
        (
            Conflict(base=b"", ours=b"A\n", theirs=b"B\n"),
            Clean(b"mid\n"),
            Conflict(base=b"", ours=b"C\n", theirs=b"D\n"),
        )
    )
    out = _run(result, b"ot")  # region 1 -> Ours, region 2 -> Theirs
    assert isinstance(out, WizardResult)
    merged = out.merged.merged()
    assert isinstance(merged, bytes)
    assert b"<<<<<<<" not in merged
    assert b"=======" not in merged
    assert b">>>>>>>" not in merged
    assert merged == b"A\nmid\nD\n"


# --------------------------------------------------------------------------- #
# Abort (Esc at the region menu)
# --------------------------------------------------------------------------- #


def test_escape_aborts_whole_run() -> None:
    assert _run(_one(), b"\x1b") is CANCEL


# --------------------------------------------------------------------------- #
# Edit sub-flow
# --------------------------------------------------------------------------- #


class _Editor:
    """Records the tempfiles it sees and applies ``action`` to each."""

    def __init__(self, action: Callable[[Path], None]) -> None:
        self._action = action
        self.seen: list[Path] = []

    def __call__(self, target: Path) -> None:
        self.seen.append(target)
        self._action(target)


def _write(content: str) -> Callable[[Path], None]:
    def _action(target: Path) -> None:
        target.write_text(content, encoding="utf-8")

    return _action


def _drive_edit(
    monkeypatch: pytest.MonkeyPatch, editor: _Editor, keys: bytes
) -> object:
    monkeypatch.setattr(W, "run_editor", editor)
    return _run(_one(b"A\n", b"B\n"), keys)


def test_edit_success_returns_edited_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    editor = _Editor(_write("MERGED\n"))
    out = _drive_edit(monkeypatch, editor, b"e")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"MERGED\n"
    # tmpfile cleaned up on the success path.
    assert editor.seen
    assert not editor.seen[0].exists()


def test_edit_leftover_markers_reprompts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Editor leaves a conflict marker → re-prompt; then the user picks Ours.
    editor = _Editor(_write("<<<<<<< OURS\nA\n=======\nB\n>>>>>>> THEIRS\n"))
    out = _drive_edit(monkeypatch, editor, b"eo")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"A\n"


def test_edit_empty_reprompts(monkeypatch: pytest.MonkeyPatch) -> None:
    editor = _Editor(_write(""))
    out = _drive_edit(monkeypatch, editor, b"et")  # re-prompt → Theirs
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"B\n"


def test_edit_called_process_error_reprompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_target: Path) -> None:
        raise CalledProcessError(1, "vi")  # user :q!

    editor = _Editor(_raise)
    out = _drive_edit(monkeypatch, editor, b"eo")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"A\n"
    assert editor.seen
    assert not editor.seen[0].exists()  # cleaned despite the raise


def test_edit_setforge_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_target: Path) -> None:
        raise SetforgeError("$EDITOR not found")

    editor = _Editor(_raise)
    with pytest.raises(SetforgeError):
        _drive_edit(monkeypatch, editor, b"e")
    assert editor.seen
    assert not editor.seen[0].exists()  # cleaned despite the raise


# --------------------------------------------------------------------------- #
# Claude-merge seam
# --------------------------------------------------------------------------- #


def test_claude_merge_callback_resolves() -> None:
    out = _run(_one(), b"c", claude_merge=lambda _c: b"CLAUDE\n")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"CLAUDE\n"


def test_claude_merge_cancel_reprompts() -> None:
    # Callback declines (CANCEL) → re-prompt the region, NOT abort; then Ours.
    out = _run(_one(b"A\n", b"B\n"), b"co", claude_merge=lambda _c: CANCEL)
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"A\n"


def test_claude_merge_default_stub_reprompts() -> None:
    # No callback injected → the default stub declines → re-prompt → Theirs.
    out = _run(_one(b"A\n", b"B\n"), b"ct")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"B\n"


# --------------------------------------------------------------------------- #
# Non-UTF-8 region
# --------------------------------------------------------------------------- #


def test_non_utf8_region_omits_edit_and_claude_merge_buttons() -> None:
    # A non-decodable side omits BOTH text flows (Edit and Claude-merge).
    conflict = Conflict(base=b"", ours=b"\xff\xfe", theirs=b"B\n")
    labels = [b.label for b in W._region_buttons(conflict)]
    assert "Edit" not in labels
    assert "Claude-merge" not in labels
    assert labels == ["Ours", "Theirs", "Skip"]


def test_non_utf8_base_omits_claude_merge_but_keeps_edit() -> None:
    # Edit needs only ours+theirs; Claude-merge also sends base, so a
    # non-decodable base gates Claude-merge off while Edit survives.
    conflict = Conflict(base=b"\xff\xfe", ours=b"A\n", theirs=b"B\n")
    labels = [b.label for b in W._region_buttons(conflict)]
    assert "Edit" in labels
    assert "Claude-merge" not in labels
    assert labels == ["Ours", "Theirs", "Edit", "Skip"]


def test_all_utf8_region_offers_claude_merge() -> None:
    conflict = Conflict(base=b"X\n", ours=b"A\n", theirs=b"B\n")
    labels = [b.label for b in W._region_buttons(conflict)]
    assert labels == ["Ours", "Theirs", "Edit", "Claude-merge", "Skip"]


def test_non_utf8_ours_is_byte_exact() -> None:
    result = MergeResult((Conflict(base=b"", ours=b"\xff\xfe", theirs=b"B\n"),))
    out = _run(result, b"o")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"\xff\xfe"


def test_render_non_utf8_does_not_raise() -> None:
    frags = W._render_conflict(Conflict(base=b"", ours=b"\xff\xfe", theirs=b"B\n"))
    text = "".join(t for _, t in frags)
    assert "�" in text  # replacement char for the undecodable bytes


# --------------------------------------------------------------------------- #
# Colour + display
# --------------------------------------------------------------------------- #


def test_render_conflict_tags_theme_roles() -> None:
    frags = W._render_conflict(Conflict(base=b"", ours=b"x\n", theirs=b"y\n"))
    styles = [s for s, _ in frags]
    assert styles == [
        "class:muted",
        "class:success",
        "class:muted",
        "class:warning",
        "class:muted",
    ]


def test_display_empty_side_placeholder() -> None:
    assert W._display(b"") == "  (empty)\n"


def test_display_sanitizes_control_chars() -> None:
    out = W._display(b"a\x00b\x1bc\n")
    assert "\x00" not in out
    assert "\x1b" not in out
    assert "^@" in out  # NUL -> caret notation


def test_display_truncates_oversize() -> None:
    big = ("line\n" * (W._MAX_DISPLAY_LINES + 10)).encode()
    out = W._display(big)
    assert "more lines" in out
    assert out.count("\n") <= W._MAX_DISPLAY_LINES + 1


def test_has_conflict_markers_predicate() -> None:
    assert W._has_conflict_markers("a\n<<<<<<< x\nb\n")
    assert W._has_conflict_markers("=======\n")
    assert not W._has_conflict_markers("clean\nmerged\n")


# --------------------------------------------------------------------------- #
# Property — INV-1 conservation + reconstruction (Ours/Theirs/Skip only)
# --------------------------------------------------------------------------- #

_KEY = {W._Choice.OURS: b"o", W._Choice.THEIRS: b"t", W._Choice.SKIP: b"s"}
_CHOICES = (W._Choice.OURS, W._Choice.THEIRS, W._Choice.SKIP)


@st.composite
def _result_and_choices(draw: st.DrawFn) -> tuple[MergeResult, list[object]]:
    segs: list[Segment] = []
    choices: list[object] = []
    for _ in range(draw(st.integers(min_value=1, max_value=5))):
        if draw(st.booleans()):
            segs.append(Clean(draw(st.binary(max_size=6))))
        else:
            segs.append(
                Conflict(
                    base=b"",
                    ours=draw(st.binary(max_size=6)),
                    theirs=draw(st.binary(max_size=6)),
                )
            )
            choices.append(draw(st.sampled_from(_CHOICES)))
    if not choices:  # guarantee at least one conflict
        segs.append(Conflict(base=b"", ours=draw(st.binary(max_size=6)), theirs=b"z"))
        choices.append(
            draw(st.sampled_from([W._Choice.OURS, W._Choice.THEIRS, W._Choice.SKIP]))
        )
    return MergeResult(tuple(segs)), choices


@pytest.mark.slow
@settings(max_examples=120, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(_result_and_choices())
def test_property_inv1_conservation(
    case: tuple[MergeResult, list[object]],
) -> None:
    result, choices = case
    keys = b"".join(_KEY[c] for c in choices)
    out = _run(result, keys)
    assert isinstance(out, WizardResult)
    assert out.merged.clean

    # Reconstruct the expected bytes: Clean passes through; a Conflict yields
    # ours for OURS/SKIP, theirs for THEIRS.
    expected = bytearray()
    sides: list[bytes] = []
    ci = 0
    for seg in result.segments:
        if isinstance(seg, Clean):
            expected += seg.bytes_
            continue
        choice = choices[ci]
        ci += 1
        chosen = seg.theirs if choice is W._Choice.THEIRS else seg.ours
        expected += chosen
        sides.append(seg.ours)
        sides.append(seg.theirs)

    merged = out.merged.merged()
    assert isinstance(merged, bytes)
    assert merged == bytes(expected)  # INV-1: lossless reconstruction
    assert out.deferred == any(c is W._Choice.SKIP for c in choices)

    # INV-1 conservation: every folded conflict side is byte-identical to an
    # original side (Ours/Theirs/Skip never mutate).
    resolved = [s.bytes_ for s in out.merged.segments if isinstance(s, Clean)]
    originals = {seg.bytes_ for seg in result.segments if isinstance(seg, Clean)}
    for r in resolved:
        assert r in originals or r in sides
