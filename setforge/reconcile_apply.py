"""Apply the 3-way reconcile engine to a single plain tracked file.

The install-side glue that activates the (otherwise dormant) reconcile
engine for **plain** tracked files — those with no ``disposition`` and no
``spans`` (files that the legacy path would deploy verbatim). Files that
carry a disposition or spans stay on the legacy ``deploy`` path until
they are migrated onto the engine (a separate follow-up).

:func:`reconcile_plain_file` is pure with respect to the filesystem: it
reads the recorded base from the store, runs :func:`setforge.reconcile.merge`
against the live + tracked content, drives the conflict wizard when needed,
and returns a :class:`ReconcileOutcome` describing what the caller should
do — it never writes the live file and never advances the store itself.
That split keeps the decision logic unit-testable and lets the caller
slot the write + ``record`` into the install pipeline's write pass.

Outcomes encode the A0 guards directly:

- ``NOOP`` — the merge is clean AND already matches live with the base
  already at tracked: an idempotent re-install writes nothing and does not
  re-record (no store churn).
- ``WRITE`` — deploy ``content`` to live and ``record`` ``new_base``.
- ``DEFERRED`` — a region was skipped in the wizard; write nothing and do
  NOT re-baseline (the unresolved upstream change must re-surface next run).
- ``CANCELLED`` — the user aborted the whole-file wizard; write nothing,
  record nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from setforge.reconcile import (
    ABSENT,
    FileId,
    MergeResult,
    merge,
    read_base,
    resolve_conflicts,
)
from setforge.reconcile.merge_model import MergeInput
from setforge.reconcile.types import Absent
from setforge.reconcile.wizard import (
    CANCEL,
    Cancelled,
    ClaudeMergeFn,
    _claude_merge_unavailable,
)

__all__ = [
    "ReconcileKind",
    "ReconcileOutcome",
    "SeedChoice",
    "SeedPrompt",
    "reconcile_plain_file",
]


class SeedChoice(StrEnum):
    """How to seed the merge base when a divergent live file has none.

    The base is recorded as the current upstream (tracked) either way — it is
    the natural common ancestor for future 3-way merges. The choice is only
    what live should hold NOW: ``KEEP_LIVE`` leaves the pre-existing live
    content in place (it becomes a local edit on top of the seeded base);
    ``TAKE_UPSTREAM`` resets live to the tracked content.
    """

    KEEP_LIVE = "keep_live"
    TAKE_UPSTREAM = "take_upstream"


# Decide the seed for one divergent file; returns CANCEL to abort the file.
type SeedPrompt = Callable[[str], SeedChoice | Cancelled]


class ReconcileKind(StrEnum):
    """What the caller should do with a :func:`reconcile_plain_file` result."""

    NOOP = "noop"
    WRITE = "write"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class ReconcileOutcome:
    """The decision for one plain tracked file.

    ``content`` and ``new_base`` are populated only for :attr:`ReconcileKind.WRITE`:
    ``content`` is the merged bytes to deploy to live (``ABSENT`` ⇒ the file
    should be removed), ``new_base`` is the upstream bytes the caller records
    as the new merge base. Both are ``None`` for the no-write kinds.
    """

    kind: ReconcileKind
    content: bytes | Absent | None = None
    new_base: bytes | None = None
    seeded: bool = False
    """True when this WRITE seeded the base from a divergent live
    NON-interactively (the safe default kept live) — the caller warns so the
    user knows their pre-existing file was kept and can adopt upstream by
    running interactively."""


def _default_seed_prompt(_display_path: str) -> SeedChoice:
    """Non-interactive seed default: keep live (never destroy a local file)."""
    return SeedChoice.KEEP_LIVE


def reconcile_plain_file(
    profile: str,
    fid: FileId,
    *,
    live: bytes | Absent,
    tracked: bytes,
    interactive: bool = False,
    display_path: str | None = None,
    claude_merge: ClaudeMergeFn = _claude_merge_unavailable,
    seed_prompt: SeedPrompt = _default_seed_prompt,
) -> ReconcileOutcome:
    """Decide how to reconcile one plain tracked file via the 3-way engine.

    ``base = read_base`` (``ABSENT`` when no base is recorded — a first
    install or a not-yet-seeded divergence), ``ours = live``, ``theirs =
    tracked``. A clean merge that already equals live with the base already
    at tracked is a :attr:`~ReconcileKind.NOOP`; any other clean merge is a
    :attr:`~ReconcileKind.WRITE` advancing the base to ``tracked``.

    A conflict is resolved interactively ONLY when ``interactive`` is set:
    the per-region wizard (:func:`~setforge.reconcile.resolve_conflicts`)
    runs, and a cancel / deferred (skipped) region writes nothing and does
    NOT re-baseline. When ``interactive`` is False (a non-TTY / ``--auto`` /
    CI install), a conflict is :attr:`~ReconcileKind.DEFERRED` without
    prompting — keeping the local file, leaving the upstream change to
    re-surface on the next interactive run, and letting the caller gate the
    exit code on the deferred count. The full-screen prompt_toolkit wizard
    must never be reached without a TTY.
    """
    base_raw = read_base(profile, fid)

    # Seed: a divergent pre-existing live file with no recorded base. Without
    # a base the 3-way would treat both sides as conflicting "adds"; instead
    # establish the upstream as the merge base and decide what live holds now.
    # Non-interactively keep live (never destroy a local file) and flag the
    # seed so the caller warns; interactively prompt for keep vs replace.
    if base_raw is None and isinstance(live, bytes) and live != tracked:
        if not interactive:
            return ReconcileOutcome(
                ReconcileKind.WRITE, content=live, new_base=tracked, seeded=True
            )
        choice = seed_prompt(display_path or str(fid))
        if choice is CANCEL:
            return ReconcileOutcome(ReconcileKind.CANCELLED)
        if choice is SeedChoice.KEEP_LIVE:
            return ReconcileOutcome(ReconcileKind.WRITE, content=live, new_base=tracked)
        return ReconcileOutcome(ReconcileKind.WRITE, content=tracked, new_base=tracked)

    base: MergeInput = ABSENT if base_raw is None else base_raw
    result: MergeResult = merge(base, live, tracked)

    if result.clean:
        merged = result.merged()
        if merged == live and base_raw == tracked:
            return ReconcileOutcome(ReconcileKind.NOOP)
        return ReconcileOutcome(ReconcileKind.WRITE, content=merged, new_base=tracked)

    if not interactive:
        return ReconcileOutcome(ReconcileKind.DEFERRED)

    wizard = resolve_conflicts(
        fid, result, display_path=display_path, claude_merge=claude_merge
    )
    if wizard is CANCEL:
        return ReconcileOutcome(ReconcileKind.CANCELLED)
    if wizard.deferred:
        return ReconcileOutcome(ReconcileKind.DEFERRED)
    return ReconcileOutcome(
        ReconcileKind.WRITE, content=wizard.merged.merged(), new_base=tracked
    )
