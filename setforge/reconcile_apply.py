"""Apply the 3-way reconcile engine to a single tracked file.

The install-side glue that activates the reconcile engine per file:
:func:`reconcile_plain_file` for **line** (text) files, and
:func:`reconcile_structured_file` for **structured** (yaml/json/jsonc) files —
the latter a key-aware sibling that merges independent-key upstream changes
clean where a line merge would false-conflict, falling back to the line path
for a genuine same-key collision. The disposition/spans cutover migrates every
deployed file onto this engine and removes the legacy ``deploy`` path.

:func:`reconcile_plain_file` does not MUTATE the filesystem: it reads the
recorded base from the store, runs :func:`setforge.reconcile.merge` against
the live + tracked content, drives the conflict wizard when needed, and
returns a :class:`ReconcileOutcome` describing what the caller should do —
it never writes the live file and never advances the store itself.
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
    Clean,
    FileId,
    MergeResult,
    merge,
    read_base,
    resolve_conflicts,
)
from setforge.reconcile.merge_model import MergeInput
from setforge.reconcile.structured_units import (
    StructuredFormat,
    _dump_model,
    _load_model,
)
from setforge.reconcile.types import Absent
from setforge.reconcile.wizard import (
    CANCEL,
    Cancelled,
    ClaudeMergeFn,
    _claude_merge_unavailable,
)
from setforge.structural_merge import merge_structural

__all__ = [
    "AutoSide",
    "ReconcileKind",
    "ReconcileOutcome",
    "SeedChoice",
    "SeedPrompt",
    "reconcile_plain_file",
    "reconcile_structured_file",
]


class AutoSide(StrEnum):
    """Non-interactive conflict resolution side (the install ``--auto`` map).

    ``OURS`` keeps the live side of every conflicting region (``--auto=keep-live``);
    ``THEIRS`` takes the tracked/upstream side (``--auto=use-tracked``). Clean
    regions always pass through either way.
    """

    OURS = "ours"
    THEIRS = "theirs"


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


def _seed_outcome(
    fid: FileId,
    *,
    live: bytes,
    tracked: bytes,
    interactive: bool,
    auto: AutoSide | None,
    display_path: str | None,
    seed_prompt: SeedPrompt,
) -> ReconcileOutcome:
    """Resolve the no-base seed for a divergent live file.

    ``--auto`` picks the side (THEIRS → upstream, OURS → keep live); else an
    interactive prompt (Keep live / Take upstream, Esc aborts); else the
    non-interactive default keeps live and flags ``seeded`` so the caller
    warns. The base is recorded from upstream either way.
    """
    if auto is AutoSide.THEIRS:
        return ReconcileOutcome(ReconcileKind.WRITE, content=tracked, new_base=tracked)
    if auto is AutoSide.OURS:
        return ReconcileOutcome(ReconcileKind.WRITE, content=live, new_base=tracked)
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


def _take_side(result: MergeResult, side: AutoSide) -> bytes:
    """Resolve every conflict region to one side; clean regions pass through.

    The non-interactive ``--auto`` resolver: each clean (agreed) region is
    kept verbatim, and each conflicting region collapses to its ``ours``
    (live) or ``theirs`` (tracked) bytes — the same per-region choice the
    wizard offers, applied uniformly without prompting.
    """
    out: list[bytes] = []
    for seg in result.segments:
        if isinstance(seg, Clean):
            out.append(seg.bytes_)
        else:  # Conflict
            out.append(seg.ours if side is AutoSide.OURS else seg.theirs)
    return b"".join(out)


def reconcile_plain_file(
    profile: str,
    fid: FileId,
    *,
    live: bytes | Absent,
    tracked: bytes,
    interactive: bool = False,
    auto: AutoSide | None = None,
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

    A conflict resolves by, in order: the per-region wizard when
    ``interactive`` (a cancel / skipped region writes nothing and does NOT
    re-baseline); else ``--auto`` (``auto`` set) collapsing every region to
    that side; else :attr:`~ReconcileKind.DEFERRED` — keeping the local file,
    leaving the upstream change to re-surface, and letting the caller gate the
    exit code. The full-screen wizard is never reached without a TTY. ``auto``
    also drives the no-base seed (OURS keeps live, THEIRS takes upstream).
    """
    base_raw = read_base(profile, fid)

    # Seed: a divergent pre-existing live file with no recorded base. Without
    # a base the 3-way would treat both sides as conflicting "adds"; instead
    # establish the upstream as the merge base and decide what live holds now:
    # --auto picks the side, else interactively prompt, else (non-interactive)
    # keep live and flag the seed so the caller warns.
    if base_raw is None and isinstance(live, bytes) and live != tracked:
        return _seed_outcome(
            fid,
            live=live,
            tracked=tracked,
            interactive=interactive,
            auto=auto,
            display_path=display_path,
            seed_prompt=seed_prompt,
        )

    base: MergeInput = ABSENT if base_raw is None else base_raw
    result: MergeResult = merge(base, live, tracked)

    if result.clean:
        merged = result.merged()
        if merged == live and base_raw == tracked:
            return ReconcileOutcome(ReconcileKind.NOOP)
        return ReconcileOutcome(ReconcileKind.WRITE, content=merged, new_base=tracked)

    if interactive:
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

    if auto is not None:
        return ReconcileOutcome(
            ReconcileKind.WRITE, content=_take_side(result, auto), new_base=tracked
        )

    return ReconcileOutcome(ReconcileKind.DEFERRED)


def reconcile_structured_file(
    profile: str,
    fid: FileId,
    *,
    live: bytes | Absent,
    tracked: bytes,
    fmt: StructuredFormat,
    interactive: bool = False,
    auto: AutoSide | None = None,
    display_path: str | None = None,
    claude_merge: ClaudeMergeFn = _claude_merge_unavailable,
    seed_prompt: SeedPrompt = _default_seed_prompt,
) -> ReconcileOutcome:
    """Decide how to reconcile one STRUCTURED (yaml/json/jsonc) tracked file.

    The key-aware sibling of :func:`reconcile_plain_file`. An independent-key
    upstream change merges CLEAN against a host edit where the line 3-way would
    false-conflict, via :func:`~setforge.structural_merge.merge_structural` over
    comment-preserving models. The base-absent seed is byte-identical to the plain
    path. A GENUINE same-key collision (``merge_structural`` reports conflicts) is
    delegated to :func:`reconcile_plain_file`, so the one proven wizard / ``--auto``
    / DEFERRED tail resolves it — no separate structured conflict UI is introduced.

    ``fmt`` is the caller-detected :class:`StructuredFormat`. Each side is parsed
    FRESH because ``merge_structural`` mutates ``ours`` (live) in place.
    """
    base_raw = read_base(profile, fid)

    # Seed a divergent pre-existing live file with no recorded base — identical to
    # the plain path (base := upstream; live decides what it holds now).
    if base_raw is None and isinstance(live, bytes) and live != tracked:
        return _seed_outcome(
            fid,
            live=live,
            tracked=tracked,
            interactive=interactive,
            auto=auto,
            display_path=display_path,
            seed_prompt=seed_prompt,
        )

    # Clean-fast-path: a key-aware 3-way over comment-preserving models.
    if base_raw is not None and isinstance(live, bytes):
        result = merge_structural(
            _load_model(base_raw, fmt),
            _load_model(live, fmt),
            _load_model(tracked, fmt),
        )
        if result.clean:
            merged = _dump_model(result.merged_model, fmt)
            if merged == live and base_raw == tracked:
                return ReconcileOutcome(ReconcileKind.NOOP)
            return ReconcileOutcome(
                ReconcileKind.WRITE, content=merged, new_base=tracked
            )

    # A genuine same-key collision (or an absent / edge live) falls back to the
    # proven line path — its wizard / --auto / DEFERRED resolves the conflict.
    return reconcile_plain_file(
        profile,
        fid,
        live=live,
        tracked=tracked,
        interactive=interactive,
        auto=auto,
        display_path=display_path,
        claude_merge=claude_merge,
        seed_prompt=seed_prompt,
    )
