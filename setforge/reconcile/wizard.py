"""Interactive per-region conflict wizard for the reconcile engine.

When A2's :func:`~setforge.reconcile.merge.merge` returns a
:class:`~setforge.reconcile.merge_model.MergeResult` that still carries
:class:`~setforge.reconcile.merge_model.Conflict` segments, a human must pick a
winner for each region. :func:`resolve_conflicts` drives that decision through
the project's :func:`~setforge.ui.widgets.button_bar` (navigable buttons, no
letter menu — UX-1) and folds the choices back into a fully-``Clean``
``MergeResult`` so :meth:`MergeResult.merged` reproduces the merged file.

This module is **pure**: it returns a resolution and writes nothing — no store
updates. It is wired into the live install/sync reconcile path via
``reconcile_apply`` and supersedes the legacy conflict wizard, removed in the
disposition/spans retirement.

Per region the wizard shows ``Ours · Theirs · [Edit] · Claude-merge · Skip``:

- **Ours / Theirs** fold that side's *raw bytes* verbatim (never re-encoded from
  the on-screen text — see :func:`_render_conflict`).
- **Skip** keeps ours and flags :attr:`WizardResult.deferred` so the caller does
  not re-baseline the file.
- **Edit** opens ``$EDITOR`` seeded with git-style markers (UTF-8 only — the
  button is omitted when *ours* or *theirs* is non-decodable).
- **Claude-merge** calls the injected :data:`ClaudeMergeFn`; the default stub
  declines (A4 supplies the real resumable ``claude -p`` session). The button is
  omitted unless *all three* sides decode as UTF-8 — the merge prompt sends
  base/ours/theirs to a text model, which cannot round-trip arbitrary bytes.

:data:`~setforge.ui.widgets.CANCEL` means "the user backed out of *this* prompt".
At the region menu that aborts the whole file (``resolve_conflicts`` returns
``CANCEL`` and the caller writes nothing); from a sub-flow (Edit / Claude-merge)
it re-prompts the same region.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from setforge._editor import run_editor
from setforge.reconcile._claude_ui import _sanitize_controls, _themed_style
from setforge.reconcile.merge_model import Clean, Conflict, MergeResult, Segment
from setforge.reconcile.types import FileId
from setforge.ui.widgets import CANCEL, Button, Cancelled, button_bar

__all__ = [
    "ClaudeMergeFn",
    "WizardResult",
    "claude_merge_unavailable",
    "resolve_conflicts",
]

#: Resolve ONE conflict region. Returns the merged bytes, or :data:`CANCEL` to
#: decline (the wizard then re-prompts that region). A4 injects the real
#: resumable ``claude -p`` session; :func:`claude_merge_unavailable` is the stub.
type ClaudeMergeFn = Callable[[Conflict], bytes | Cancelled]

#: Styled-fragment list — prompt_toolkit's ``(style_class, text)`` shape.
type _Fragments = list[tuple[str, str]]

# Git-conflict marker lines. The descriptive suffix is display sugar; the pinned
# leftover-marker predicate keys off the 7-char prefixes (see _has_conflict_markers).
_OURS_MARKER = "<<<<<<< OURS (this host)"
_SEP_MARKER = "======="
_THEIRS_MARKER = ">>>>>>> THEIRS (upstream)"

#: Above this many lines a rendered side is truncated (display only — the folded
#: bytes are always the full raw side).
_MAX_DISPLAY_LINES = 40


def claude_merge_unavailable(_conflict: Conflict) -> Cancelled:
    """Default Claude-merge stub: always declines (re-prompts the region).

    The public default-arg sentinel shared by the install-side reconcile
    callers (``reconcile_apply`` / ``cli._install_helpers``): a
    non-interactive / ``--auto`` / CI run wires this so Claude-merge is
    never auto-invoked.
    """
    return CANCEL


#: Back-compat private alias — kept pointing at the public function so existing
#: ``from ... import _claude_merge_unavailable`` imports (and identity checks
#: against them) keep working after the promotion to a public name.
_claude_merge_unavailable = claude_merge_unavailable


@dataclass(frozen=True, slots=True)
class WizardResult:
    """A resolved file plus a re-baseline guard.

    ``merged`` is fully ``Clean`` — every ``Conflict`` was replaced by a
    ``Clean`` of the chosen bytes — so ``merged.merged()`` yields the final
    file. ``deferred`` is True iff at least one region was *Skipped* (kept ours
    unresolved); the caller must then NOT re-baseline the file this run.
    """

    merged: MergeResult
    deferred: bool


class _Choice(StrEnum):
    """The button a user picked for one conflict region."""

    OURS = "ours"
    THEIRS = "theirs"
    EDIT = "edit"
    CLAUDE_MERGE = "claude_merge"
    SKIP = "skip"


# ---------------------------------------------------------------------------
# Display — str/fragments, NEVER fed back into the resolved bytes.
# ---------------------------------------------------------------------------


def _both_utf8(conflict: Conflict) -> bool:
    """Whether both sides decode as UTF-8 (gate for the Edit button)."""
    try:
        conflict.ours.decode("utf-8")
        conflict.theirs.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _all_utf8(conflict: Conflict) -> bool:
    """Whether base, ours, AND theirs decode as UTF-8 (gate for Claude-merge).

    Claude-merge sends all three sides to a text model (the prompt includes
    ``base`` so it can honour what upstream intended), so a single non-decodable
    side omits the button — a text LLM can't round-trip arbitrary bytes.
    """
    try:
        conflict.base.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return _both_utf8(conflict)


def _truncate(text: str) -> str:
    """Cap the display at :data:`_MAX_DISPLAY_LINES` lines with a marker."""
    lines = text.splitlines(keepends=True)
    if len(lines) <= _MAX_DISPLAY_LINES:
        return text
    kept = "".join(lines[:_MAX_DISPLAY_LINES])
    if not kept.endswith("\n"):
        kept += "\n"
    return f"{kept}… ({len(lines) - _MAX_DISPLAY_LINES} more lines)\n"


def _display(side: bytes) -> str:
    """Render one conflict side for the panel (decode-lossy, display-only).

    Empty renders as ``(empty)``; a trailing newline is ensured so the next
    marker line never fuses onto the last content line. The result is shown
    only — the fold always uses the raw ``side`` bytes.
    """
    if side == b"":
        return "  (empty)\n"
    text = _sanitize_controls(side.decode("utf-8", errors="replace"))
    if not text.endswith("\n"):
        text += "\n"
    return _truncate(text)


def _render_conflict(conflict: Conflict) -> _Fragments:
    """Build the coloured git-style body fragments for one conflict region."""
    return [
        ("class:muted", f"{_OURS_MARKER}\n"),
        ("class:success", _display(conflict.ours)),
        ("class:muted", f"{_SEP_MARKER}\n"),
        ("class:warning", _display(conflict.theirs)),
        ("class:muted", _THEIRS_MARKER),
    ]


def _title(path: str, index: int, total: int) -> str:
    """Frame title, e.g. ``~/.claude/CLAUDE.md — region 2 of 3``."""
    return f"{path} — region {index} of {total}"


def _region_buttons(conflict: Conflict) -> list[Button[_Choice]]:
    """Buttons for one region.

    Edit is offered only when ours+theirs decode as UTF-8; Claude-merge only
    when all three sides (incl. base) decode — both gates omit a button a text
    flow can't honour on a non-decodable region.
    """
    buttons = [Button("Ours", _Choice.OURS), Button("Theirs", _Choice.THEIRS)]
    if _both_utf8(conflict):
        buttons.append(Button("Edit", _Choice.EDIT))
    if _all_utf8(conflict):
        buttons.append(Button("Claude-merge", _Choice.CLAUDE_MERGE))
    buttons.append(Button("Skip", _Choice.SKIP))
    return buttons


# ---------------------------------------------------------------------------
# Edit sub-flow.
# ---------------------------------------------------------------------------


def _marker_text(conflict: Conflict) -> str:
    """Seed text for the Edit buffer — git markers wrapping both sides.

    Precondition: both sides decode as UTF-8 (guaranteed by the Edit-button
    gate). Each side is given a trailing newline so the markers sit on their
    own lines.
    """
    ours = conflict.ours.decode("utf-8")
    theirs = conflict.theirs.decode("utf-8")
    if ours and not ours.endswith("\n"):
        ours += "\n"
    if theirs and not theirs.endswith("\n"):
        theirs += "\n"
    return f"{_OURS_MARKER}\n{ours}{_SEP_MARKER}\n{theirs}{_THEIRS_MARKER}\n"


def _has_conflict_markers(text: str) -> bool:
    """Whether any line still begins with a git-conflict marker (pinned predicate)."""
    return any(
        line.startswith(("<<<<<<<", "=======", ">>>>>>>")) for line in text.splitlines()
    )


def _edit_region(conflict: Conflict) -> bytes | Cancelled:
    """Resolve a region via ``$EDITOR``; return merged bytes or :data:`CANCEL`.

    Seeds a UTF-8 tempfile with git markers, opens the editor, and reads the
    result back. A benign editor abort (``:q!`` → ``CalledProcessError``) or a
    non-UTF-8 read-back re-prompts (``CANCEL``); a config fault (missing
    ``$EDITOR`` etc. → ``SetforgeError`` from :func:`run_editor`) propagates.
    Empty output or surviving markers also re-prompts. The tempfile is unlinked
    on every editor outcome — success, abort, or non-UTF-8 read-back.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", encoding="utf-8", delete=False
    ) as handle:
        handle.write(_marker_text(conflict))
        tmp_path = Path(handle.name)
    try:
        run_editor(tmp_path)
        text = tmp_path.read_text(encoding="utf-8")
    except (subprocess.CalledProcessError, UnicodeDecodeError):
        return CANCEL
    finally:
        tmp_path.unlink(missing_ok=True)
    if text == "" or _has_conflict_markers(text):
        return CANCEL
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# Region loop + entry point.
# ---------------------------------------------------------------------------


def _resolve_region(
    conflict: Conflict,
    *,
    path: str,
    index: int,
    total: int,
    claude_merge: ClaudeMergeFn,
) -> tuple[bytes, bool] | Cancelled:
    """Drive one region to a ``(chosen_bytes, was_skipped)`` outcome.

    Returns :data:`CANCEL` only when the user backs out of the region menu
    itself (whole-file abort). Backing out of Edit / Claude-merge re-prompts.
    """
    while True:
        choice = button_bar(
            _region_buttons(conflict),
            title=_title(path, index, total),
            body=_render_conflict(conflict),
            style=_themed_style(),
        )
        if choice is CANCEL:
            return CANCEL
        if choice is _Choice.OURS:
            return (conflict.ours, False)
        if choice is _Choice.THEIRS:
            return (conflict.theirs, False)
        if choice is _Choice.SKIP:
            return (conflict.ours, True)
        if choice is _Choice.EDIT:
            edited = _edit_region(conflict)
            if edited is CANCEL:
                continue
            return (edited, False)
        # _Choice.CLAUDE_MERGE
        merged = claude_merge(conflict)
        if merged is CANCEL:
            continue
        return (merged, False)


def resolve_conflicts(
    file_id: FileId,
    result: MergeResult,
    *,
    display_path: str | None = None,
    claude_merge: ClaudeMergeFn = _claude_merge_unavailable,
) -> WizardResult | Cancelled:
    """Drive the per-region wizard over ``result``'s conflicts, in order.

    ``file_id`` (or ``display_path``, if given) names the file in the frame
    title. Returns a :class:`WizardResult` whose ``merged`` is fully ``Clean``,
    or :data:`CANCEL` if the user aborts at a region menu (the caller then
    writes nothing).

    Precondition: ``result`` carries at least one conflict — the caller checks
    ``not result.clean`` first; a clean result raises :class:`ValueError`.
    Propagates :class:`~setforge.errors.SetforgeError` if an Edit sub-flow hits
    an editor-config fault (missing / malformed ``$EDITOR``, timeout).
    """
    if result.clean:
        raise ValueError("resolve_conflicts called on a conflict-free result")

    path = display_path or str(file_id)
    total = sum(1 for seg in result.segments if isinstance(seg, Conflict))
    out: list[Segment] = []
    deferred = False
    index = 0
    for seg in result.segments:
        if isinstance(seg, Clean):
            out.append(seg)  # INV-6: clean runs pass through byte-identical
            continue
        index += 1
        outcome = _resolve_region(
            seg, path=path, index=index, total=total, claude_merge=claude_merge
        )
        if outcome is CANCEL:
            return CANCEL
        chosen, was_skip = outcome
        out.append(Clean(chosen))
        deferred = deferred or was_skip
    return WizardResult(MergeResult(tuple(out), absent=result.absent), deferred)
