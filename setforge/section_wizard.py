"""Install-time interactive wizard for `shared` user-section drift.

Resolves per-section drift surfaced by
:func:`setforge.section_reconcile.classify_section_drift` into a final
body string per section. Three modes:

- ``--reconcile-user-sections`` (interactive) — prompt per drifted
  shared section:
  ``[k]eep live / [t]ake tracked / [e]dit / [s]kip / [q]uit-keep-rest``.
- ``--auto=use-tracked`` — silent forceful overwrite of every shared
  section with tracked body (bypasses three-way classification, used
  for scripted "deploy tracked-side updates" runs).
- ``--auto=keep-live`` — silent keep-live, no warning.

Host-local sections always silently keep live; the wizard never
surfaces them.

POSIX-only: the editor sub-action shells out to ``$EDITOR`` (default
``vi``); the single-keypress prompter is delegated to
:func:`setforge.wizard.read_one_choice` so prompts behave identically
to the existing install / sync wizards.
"""

from __future__ import annotations

import difflib
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from rich.console import Console
from rich.syntax import Syntax

from setforge._editor import run_editor
from setforge.section_reconcile import (
    SectionDrift,
    SectionDriftState,
)
from setforge.sections import SectionSemantics
from setforge.wizard import read_one_choice

__all__ = [
    "ReconcileAuto",
    "SectionAction",
    "SectionDecision",
    "reconcile_sections",
    "state_label",
]


class ReconcileAuto(StrEnum):
    """Closed set of non-interactive resolutions for install reconcile."""

    USE_TRACKED = "use-tracked"
    KEEP_LIVE = "keep-live"


class SectionAction(StrEnum):
    """Closed set of per-section outcomes from the wizard."""

    KEEP_LIVE = "keep_live"
    TAKE_TRACKED = "take_tracked"
    EDIT = "edit"
    SKIP = "skip"
    QUIT_KEEP_REST = "quit_keep_rest"
    PROMOTE = "promote"


# Resolved decision per section: the final body to write + the action
# the wizard took. ``body`` is what the install path should splice into
# the rendered output for this section name; ``action`` is the audit
# trail (informational; not persisted yet).
@dataclass(slots=True, frozen=True)
class SectionDecision:
    """Result for one section after wizard resolution."""

    name: str
    body: str
    action: SectionAction


def reconcile_sections(
    drifts: Mapping[str, SectionDrift],
    *,
    auto: ReconcileAuto | None,
    interactive: bool,
    console: Console | None = None,
) -> dict[str, SectionDecision]:
    """Resolve every section in ``drifts`` to a :class:`SectionDecision`.

    Parameters
    ----------
    drifts:
        Output of :func:`classify_section_drift`. Iteration order is the
        deterministic insertion order ``classify_section_drift``
        guarantees (extract_sections order).
    auto:
        Non-interactive resolution mode. ``USE_TRACKED`` overwrites
        every shared section with tracked body; ``KEEP_LIVE`` keeps
        every shared section's live body. host-local is always
        keep-live regardless of ``auto``.
    interactive:
        When ``True`` and ``auto`` is ``None``, prompts the user per
        drifted shared section. When ``False`` and ``auto`` is
        ``None``, defaults to keep-live for every shared section (the
        bare-install default — quiet behaviour for the live file
        itself; the install path is responsible for the warning line).
    console:
        Rich Console for prompt rendering (defaults to a fresh
        ``Console()``).

    Returns
    -------
    Decision per section in the same iteration order as ``drifts``.
    Sections with :attr:`SectionDriftState.NO_DRIFT` keep their live
    body silently. Host-local sections always keep their live body.
    """
    if console is None:
        console = Console()

    out: dict[str, SectionDecision] = {}
    quit_remaining = False
    for name, drift in drifts.items():
        decision = _resolve_one_drift(
            drift,
            auto=auto,
            interactive=interactive,
            quit_remaining=quit_remaining,
            console=console,
        )
        out[name] = decision
        if decision.action is SectionAction.QUIT_KEEP_REST:
            quit_remaining = True
    return out


def _resolve_one_drift(
    drift: SectionDrift,
    *,
    auto: ReconcileAuto | None,
    interactive: bool,
    quit_remaining: bool,
    console: Console,
) -> SectionDecision:
    """Pick the outcome for one drift given auto/interactive mode.

    Mirrors the precedence in :func:`reconcile_sections`: host-local
    and no-drift always keep live; ``auto`` overrides interactive; a
    prior ``QUIT_KEEP_REST`` or a non-interactive run falls through to
    silent keep-live; otherwise prompt.
    """
    if drift.semantics is SectionSemantics.HOST_LOCAL:
        return SectionDecision(drift.name, drift.live_body, SectionAction.KEEP_LIVE)
    if drift.state is SectionDriftState.NO_DRIFT:
        return SectionDecision(drift.name, drift.live_body, SectionAction.KEEP_LIVE)
    if auto is ReconcileAuto.USE_TRACKED:
        return SectionDecision(
            drift.name, drift.tracked_body, SectionAction.TAKE_TRACKED
        )
    if auto is ReconcileAuto.KEEP_LIVE:
        return SectionDecision(drift.name, drift.live_body, SectionAction.KEEP_LIVE)
    if not interactive or quit_remaining:
        # Bare install or post-quit: keep-live silently (install path
        # emits the aggregate warning).
        return SectionDecision(drift.name, drift.live_body, SectionAction.KEEP_LIVE)
    return _prompt_one(drift, console)


def _prompt_one(drift: SectionDrift, console: Console) -> SectionDecision:
    """Render the per-section prompt for ``drift`` and return a decision."""
    _render_header(drift, console)
    _render_diff(drift, console)
    _render_choices(console)
    choice = read_one_choice("   Choice (k/t/e/s/q): ", {"k", "t", "e", "s", "q"})
    if choice == "k":
        return SectionDecision(drift.name, drift.live_body, SectionAction.KEEP_LIVE)
    if choice == "t":
        return SectionDecision(
            drift.name, drift.tracked_body, SectionAction.TAKE_TRACKED
        )
    if choice == "s":
        return SectionDecision(drift.name, drift.live_body, SectionAction.SKIP)
    if choice == "q":
        return SectionDecision(
            drift.name, drift.live_body, SectionAction.QUIT_KEEP_REST
        )
    # 'e' — open $EDITOR on a tmpfile seeded with the live body
    edited = _edit_body(drift)
    return SectionDecision(drift.name, edited, SectionAction.EDIT)


def _render_header(drift: SectionDrift, console: Console) -> None:
    sep = "─" * 57
    console.print(f"\n[dim]{sep}[/dim]")
    console.print(
        f" [bold]section[/bold] [cyan]{drift.name}[/cyan]  "
        f"[dim]({drift.semantics.value})[/dim]  "
        f"[yellow]{state_label(drift.state)}[/yellow]"
    )
    console.print(f"[dim]{sep}[/dim]")


def state_label(state: SectionDriftState) -> str:
    """Human-readable state label rendered in the prompt header.

    The exact label text is part of the contract — Docker e2e tests
    grep stdout for ``"pending tracked update"`` (variant 2 from the
    bd --notes) and ``"three-way"`` (variant 18). Changes here must
    keep the substrings the tests assert against.
    """
    mapping = {
        SectionDriftState.NO_DRIFT: "no drift",
        SectionDriftState.LEGACY: "legacy (no embedded hash)",
        SectionDriftState.PENDING_TRACKED: "pending tracked update",
        SectionDriftState.LIVE_EDITED: "live edits",
        SectionDriftState.CONFLICT: "three-way conflict",
        SectionDriftState.INCONSISTENT: "inconsistent (treated as conflict)",
    }
    return mapping[state]


def _render_diff(drift: SectionDrift, console: Console) -> None:
    """Render a unified diff between live and tracked bodies."""
    diff = "".join(
        difflib.unified_diff(
            drift.live_body.splitlines(keepends=True),
            drift.tracked_body.splitlines(keepends=True),
            fromfile=f"live/{drift.name}",
            tofile=f"tracked/{drift.name}",
        )
    )
    if diff:
        console.print(Syntax(diff, "diff"))
    else:
        console.print("[dim](bodies identical — odd; classifier may be stale)[/dim]")


def _render_choices(console: Console) -> None:
    console.print("")
    console.print(
        "   [bold][[k]][/bold] keep live           "
        "[dim](preserve current live body)[/dim]"
    )
    console.print(
        "   [bold][[t]][/bold] take tracked        "
        "[dim](overwrite live with tracked body)[/dim]"
    )
    console.print(
        "   [bold][[e]][/bold] edit                "
        "[dim](open $EDITOR with live body as seed)[/dim]"
    )
    console.print(
        "   [bold][[s]][/bold] skip                "
        "[dim](keep live, ask again next install)[/dim]"
    )
    console.print(
        "   [bold][[q]][/bold] quit-keep-rest      "
        "[dim](keep live for this and all remaining)[/dim]"
    )
    console.print("")


def _edit_body(drift: SectionDrift) -> str:
    """Open ``$EDITOR`` on a tmpfile pre-seeded with the live body.

    Returns the edited text. Suffix ``.md`` so editors pick up Markdown
    syntax highlighting — every user-section tracked_file that ships today
    is Markdown.
    """
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".md", encoding="utf-8"
    ) as fh:
        fh.write(drift.live_body)
        tmp_path = Path(fh.name)
    try:
        run_editor(tmp_path)
        return tmp_path.read_text(encoding="utf-8")
    finally:
        tmp_path.unlink(missing_ok=True)


def format_drift_summary(drifts: Iterable[SectionDrift]) -> str:
    """Render the bare-install aggregate warning line.

    Counts pending-tracked / live-edited / conflict / legacy /
    inconsistent across all shared sections. host-local sections and
    no-drift sections are excluded.

    Iterates ``SectionDriftState`` enum order and emits a fragment per
    non-empty group via :func:`_format_drift_group` (which suppresses
    empty groups).

    Example:
      ``"4 shared sections drifted: 1 legacy (no embedded hash),
      1 pending tracked update, 1 live edit, 1 three-way conflict"``

    Empty string when nothing to warn about — caller suppresses the
    warning line entirely in that case.
    """
    counts: Counter[SectionDriftState] = Counter(
        d.state
        for d in drifts
        if d.semantics is SectionSemantics.SHARED
        and d.state is not SectionDriftState.NO_DRIFT
    )
    if not counts:
        return ""
    parts = [
        fragment
        for state in SectionDriftState
        if (count := counts[state]) and (fragment := _format_drift_group(state, count))
    ]
    total = sum(counts.values())
    return f"{total} shared section{'s' if total != 1 else ''} drifted: " + ", ".join(
        parts
    )


def _format_drift_group(state: SectionDriftState, count: int) -> str:
    """Render the per-state summary fragment for ``state``.

    Exhaustive dispatcher on :class:`SectionDriftState` — the
    ``case _: assert_never(state)`` fall-through pairs with mypy's
    closed-enum check so a newly-added enum member is a compile-time
    AND runtime error rather than a silent drop from summary output.

    Returns the fragment ``"N <label>"`` for ``state`` (e.g.
    ``"2 pending tracked updates"``). Pluralises the pending /
    live-edited / conflict labels; legacy and inconsistent use a fixed
    label.
    """
    match state:
        case SectionDriftState.NO_DRIFT:
            # NO_DRIFT contributes no summary output. Caller pre-filters
            # NO_DRIFT entries; this case stays as the dispatcher's
            # explicit suppression contract (not dead code).
            return ""
        case SectionDriftState.PENDING_TRACKED:
            return f"{count} pending tracked update{'s' if count != 1 else ''}"
        case SectionDriftState.LIVE_EDITED:
            return f"{count} live edit{'s' if count != 1 else ''}"
        case SectionDriftState.CONFLICT:
            return f"{count} three-way conflict{'s' if count != 1 else ''}"
        case SectionDriftState.LEGACY:
            return f"{count} legacy (no embedded hash)"
        case SectionDriftState.INCONSISTENT:
            return f"{count} inconsistent"
        case _:
            assert_never(state)
