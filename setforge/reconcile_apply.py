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

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from setforge.errors import (
    DuplicateKeyInMergeModel,
    MergeTypeMismatch,
    StructuredParseError,
)
from setforge.reconcile import (
    ABSENT,
    Clean,
    FileId,
    MergeResult,
    merge,
    read_base,
    read_local,
)
from setforge.reconcile.conflict_choices import (
    ClaudeMergeFn,
    claude_merge_unavailable,
)
from setforge.reconcile.merge_model import MergeInput
from setforge.reconcile.structured_units import (
    StructuredFormat,
    _dump_model,
    _load_model,
    models_equal,
    splice_lines_toward,
    uses_aliases,
)
from setforge.reconcile.types import Absent
from setforge.structural_merge import merge_structural
from setforge.ui.primitives import CANCEL, Cancelled

if TYPE_CHECKING:
    from setforge.reconcile.wizard import WizardResult

__all__ = [
    "AutoSide",
    "ReconcileAuto",
    "ReconcileKind",
    "ReconcileOutcome",
    "SeedChoice",
    "SeedPrompt",
    "reconcile_plain_file",
    "reconcile_structured_file",
]


class ReconcileAuto(StrEnum):
    """Closed set of non-interactive resolutions for install reconcile.

    The CLI ``--auto`` surface for ``--reconcile-user-sections``: ``USE_TRACKED``
    deploys tracked-side updates over the live body, ``KEEP_LIVE`` keeps live.
    Co-located with :class:`AutoSide` (the per-region merge side it maps onto).
    """

    USE_TRACKED = "use-tracked"
    KEEP_LIVE = "keep-live"


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
type SeedPrompt = Callable[[str, bytes, bytes], SeedChoice | Cancelled]


def resolve_conflicts(
    file_id: FileId,
    result: MergeResult,
    *,
    display_path: str | None = None,
    claude_merge: ClaudeMergeFn = claude_merge_unavailable,
) -> WizardResult | Cancelled:
    """Load the interactive conflict UI only when reconciliation needs it."""
    from setforge.reconcile.wizard import resolve_conflicts as resolve

    return resolve(
        file_id,
        result,
        display_path=display_path,
        claude_merge=claude_merge,
    )


class ReconcileKind(StrEnum):
    """What the caller should do with a :func:`reconcile_plain_file` result."""

    NOOP = "noop"
    WRITE = "write"
    REMOVE = "remove"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class ReconcileOutcome:
    """The decision for one plain tracked file."""

    kind: ReconcileKind
    content: bytes | Absent | None = None
    new_base: bytes | None = None
    seeded: bool = False
    """True when this WRITE seeded the base from a divergent live
    NON-interactively (the safe default kept live) — the caller warns so the
    user knows their pre-existing file was kept and can adopt upstream by
    running interactively."""


def _default_seed_prompt(
    _display_path: str, _live: bytes, _tracked: bytes
) -> SeedChoice:
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
    choice = seed_prompt(display_path or str(fid), live, tracked)
    if choice is CANCEL:
        return ReconcileOutcome(ReconcileKind.CANCELLED)
    if choice is SeedChoice.KEEP_LIVE:
        return ReconcileOutcome(ReconcileKind.WRITE, content=live, new_base=tracked)
    return ReconcileOutcome(ReconcileKind.WRITE, content=tracked, new_base=tracked)


def _take_side(result: MergeResult, side: AutoSide) -> MergeInput:
    """Resolve every conflict region to one side; clean regions pass through.

    The non-interactive ``--auto`` resolver: each clean (agreed) region is
    kept verbatim, and each conflicting region collapses to its ``ours``
    (live) or ``theirs`` (tracked) bytes — the same per-region choice the
    wizard offers, applied uniformly without prompting.
    """
    if side is AutoSide.OURS and result.ours_absent:
        return ABSENT
    if side is AutoSide.THEIRS and result.theirs_absent:
        return ABSENT

    out: list[bytes] = []
    for seg in result.segments:
        if isinstance(seg, Clean):
            out.append(seg.bytes_)
        else:  # Conflict
            out.append(seg.ours if side is AutoSide.OURS else seg.theirs)
    return b"".join(out)


def _resolved_outcome(content: MergeInput, *, new_base: bytes) -> ReconcileOutcome:
    """Map an explicitly resolved file image without collapsing absence."""
    kind = ReconcileKind.REMOVE if content is ABSENT else ReconcileKind.WRITE
    return ReconcileOutcome(kind, content=content, new_base=new_base)


def _selected_absence(result: MergeResult, selections: tuple[str | None, ...]) -> bool:
    """Whether a whole-file resolution selected its originally absent side."""
    if len(selections) != 1:
        return False
    return (selections[0] == AutoSide.OURS and result.ours_absent) or (
        selections[0] == AutoSide.THEIRS and result.theirs_absent
    )


def _wizard_content(result: MergeResult, wizard: WizardResult) -> MergeInput:
    """Restore the file-level identity hidden by the wizard's byte rendering."""
    if _selected_absence(result, wizard.selections):
        return ABSENT
    return wizard.merged.merged()


def _absence_outcome(
    profile: str,
    fid: FileId,
    base_raw: bytes | None,
    tracked: bytes,
    auto: AutoSide | None,
) -> ReconcileOutcome:
    """Resolve a clean merge to absence: honor it, or restore under use-tracked."""
    if auto is AutoSide.THEIRS:
        return ReconcileOutcome(ReconcileKind.WRITE, content=tracked, new_base=tracked)
    if read_local(profile, fid) is ABSENT and base_raw == tracked:
        return ReconcileOutcome(ReconcileKind.NOOP)
    return ReconcileOutcome(ReconcileKind.REMOVE, content=ABSENT, new_base=tracked)


def reconcile_plain_file(
    profile: str,
    fid: FileId,
    *,
    live: bytes | Absent,
    tracked: bytes,
    interactive: bool = False,
    auto: AutoSide | None = None,
    display_path: str | None = None,
    claude_merge: ClaudeMergeFn = claude_merge_unavailable,
    seed_prompt: SeedPrompt = _default_seed_prompt,
) -> ReconcileOutcome:
    """Decide how to reconcile one plain tracked file via the 3-way engine.

    ``base = read_base`` (``ABSENT`` when no base is recorded — a first
    install or a not-yet-seeded divergence), ``ours = live``, ``theirs =
    tracked``. A clean merge that already equals live with the base already
    at tracked is a :attr:`~ReconcileKind.NOOP`; a clean merge resolving to
    absence is a :attr:`~ReconcileKind.REMOVE` (unlink live, record
    ``local=ABSENT``); any other clean merge is a :attr:`~ReconcileKind.WRITE`
    advancing the base to ``tracked``.

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
        if result.absent:
            return _absence_outcome(profile, fid, base_raw, tracked, auto)
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
        return _resolved_outcome(_wizard_content(result, wizard), new_base=tracked)

    if auto is not None:
        return _resolved_outcome(_take_side(result, auto), new_base=tracked)

    return ReconcileOutcome(ReconcileKind.DEFERRED)


def _parses(data: bytes, fmt: StructuredFormat) -> bool:
    """Whether ``data`` loads as a single ``fmt`` document."""
    try:
        _load_model(data, fmt)
    except StructuredParseError:
        return False
    return True


def _line_merge_agreeing(
    base: bytes, live: bytes, tracked: bytes, model: object, fmt: StructuredFormat
) -> bytes | None:
    """The line 3-way of the three texts when it holds exactly ``model``'s values.

    The line merge keeps every untouched line byte-identical (INV-6) and carries
    text-only edits (comments, layout) the key merge cannot see, but it is blind
    to keys: it is trusted only when it is clean AND parses to the same values
    as the clean key-aware merge. ``None`` otherwise.
    """
    lines = merge(base, live, tracked)
    if not lines.clean:
        return None
    text = lines.merged()
    if not isinstance(text, bytes):
        return None
    try:
        agrees = models_equal(_load_model(text, fmt), model)
    except (MergeTypeMismatch, DuplicateKeyInMergeModel, StructuredParseError):
        return None
    return text if agrees else None


def _is_strict_json(data: bytes) -> bool:
    try:
        json.loads(data)
    except ValueError:
        return False
    return True


def _loses_strictness(
    text: bytes, live: bytes, tracked: bytes, fmt: StructuredFormat
) -> bool:
    """Whether ``text`` needs JSON5 syntax neither strict-JSON source uses.

    Lines joined from two strict JSON files can leave a trailing comma, which
    the json5 loader accepts but a strict consumer of the file rejects.
    """
    return (
        fmt is StructuredFormat.JSONC
        and _is_strict_json(live)
        and _is_strict_json(tracked)
        and not _is_strict_json(text)
    )


def _key_merge(
    base: bytes, live: bytes, tracked: bytes, fmt: StructuredFormat
) -> bytes | None:
    """The clean key-aware 3-way of three structured texts, or ``None``.

    ``None`` on a same-key conflict and for a side the key engine cannot model
    (unparseable, multi-document, duplicate or non-string keys, a non-mapping
    root, YAML aliases / merge keys) — the caller line-merges those. Each side
    is parsed FRESH because ``merge_structural`` mutates ``ours`` (live) in place.
    """
    try:
        if fmt is StructuredFormat.YAML and any(
            uses_aliases(side) for side in (base, live, tracked)
        ):
            return None
        result = merge_structural(
            _load_model(base, fmt), _load_model(live, fmt), _load_model(tracked, fmt)
        )
        if not result.clean:
            return None
        model = result.merged_model
        merged = _line_merge_agreeing(base, live, tracked, model, fmt)
        if merged is None or _loses_strictness(merged, live, tracked, fmt):
            merged = splice_lines_toward(live, tracked, model, fmt)
        if merged is None or _loses_strictness(merged, live, tracked, fmt):
            merged = _dump_model(model, fmt)
    except (MergeTypeMismatch, DuplicateKeyInMergeModel, StructuredParseError):
        return None
    return merged


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
    claude_merge: ClaudeMergeFn = claude_merge_unavailable,
    seed_prompt: SeedPrompt = _default_seed_prompt,
) -> ReconcileOutcome:
    """Decide how to reconcile one STRUCTURED (yaml/json/jsonc) tracked file.

    The key-aware sibling of :func:`reconcile_plain_file`. An independent-key
    upstream change merges CLEAN against a host edit where the line 3-way would
    false-conflict, via :func:`~setforge.structural_merge.merge_structural` over
    comment-preserving models. The key merge decides the VALUES; the bytes are the
    line 3-way's whenever that is clean and holds the same values, else live with
    the tracked line regions that realise those values (untouched lines stay
    byte-identical either way), else the re-serialised model. When one side did
    not move
    the other side's bytes are used verbatim. The base-absent seed is byte-identical
    to the plain path. A GENUINE same-key collision (``merge_structural`` reports
    conflicts) is delegated to :func:`reconcile_plain_file`, so the one proven
    wizard / ``--auto`` / DEFERRED tail resolves it — no separate structured
    conflict UI is introduced.

    ``fmt`` is the caller-detected :class:`StructuredFormat`.
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

    if base_raw is not None and isinstance(live, bytes):
        # A live file the format cannot parse (truncated write, editor crash)
        # has no keys to merge; --auto=use-tracked restores the tracked file.
        if (
            auto is AutoSide.THEIRS
            and live != tracked
            and not _parses(live, fmt)
            and _parses(tracked, fmt)
        ):
            return ReconcileOutcome(
                ReconcileKind.WRITE, content=tracked, new_base=tracked
            )

        # Nothing to merge: one side did not move, or both already agree. The
        # source bytes stand verbatim — a model round-trip would reformat them.
        if live == tracked or tracked == base_raw:
            if base_raw == tracked:
                return ReconcileOutcome(ReconcileKind.NOOP)
            return ReconcileOutcome(ReconcileKind.WRITE, content=live, new_base=tracked)
        if live == base_raw:
            return ReconcileOutcome(
                ReconcileKind.WRITE, content=tracked, new_base=tracked
            )

        merged = _key_merge(base_raw, live, tracked, fmt)
        if merged is not None:
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
