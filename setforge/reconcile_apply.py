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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, cast

from json5.model import JSONArray, Node
from json5.model import walk as json5_walk
from patiencediff import PatienceSequenceMatcher

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
from setforge.reconcile.merge import _MaxRecursionDepth, split_lines
from setforge.reconcile.merge_model import MergeInput
from setforge.reconcile.structured_units import (
    StructuredFormat,
    _dump_model,
    load_quietly,
    models_equal,
    uses_aliases,
)
from setforge.reconcile.types import Absent
from setforge.structural_merge import get_at_path, merge_structural
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
        load_quietly(data, fmt)
    except StructuredParseError:
        return False
    return True


def _terminated(data: bytes, newline: bytes) -> bytes:
    """``data`` with ``newline`` ending its last line when nothing does."""
    if not data or data.endswith(b"\n"):
        return data
    return data + newline


#: Largest ours-times-theirs count of edits in one conflict hunk that is
#: checked for collisions; a bigger hunk stays a conflict (bounded cost).
_MAX_EDIT_PAIRS: Final = 4096

type _Edit = tuple[int, int, int, int]


def _edits(base: Sequence[object], side: Sequence[object]) -> list[_Edit]:
    """The ``(base start, base end, side start, side end)`` ranges ``side`` changed.

    A run replaced by a run of the same length is one edit per element, so an
    element only one side touched is not held back by its neighbour.
    """
    edits: list[_Edit] = []
    matcher = PatienceSequenceMatcher(None, base, side)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i2 - i1 == j2 - j1:
            edits.extend(
                (i1 + k, i1 + k + 1, j1 + k, j1 + k + 1) for k in range(i2 - i1)
            )
        else:
            edits.append((i1, i2, j1, j2))
    return edits


def _edits_collide(left: _Edit, right: _Edit) -> bool:
    """Whether two sides' edits claim the same place in the base sequence.

    Two replaced ranges collide when they intersect, and an insertion collides
    with a range it falls strictly inside. An insertion at the EDGE of a range
    is independent of it, and so are two insertions at the same point (the
    caller decides whether their order matters).
    """
    (a1, a2, _, _), (b1, b2, _, _) = left, right
    if a1 == a2:
        return b1 < a1 < b2
    if b1 == b2:
        return a1 < b1 < a2
    return a1 < b2 and b1 < a2


def _repeats[T](
    edit: _Edit, side: Sequence[T], others: list[_Edit], other_side: Sequence[T]
) -> bool:
    """Whether ``edit`` inserts what the other side's edit beside it already has.

    Both sides added the same line at one spot, but one of them also changed
    the line next to it: its diff is a single replacement that starts (or ends)
    with that line, the other side's is a pure insertion at the replacement's
    edge. They do not collide, yet applying both would write the line twice.
    """
    at, end, j1, j2 = edit
    if at != end:
        return False
    new = side[j1:j2]
    return any(
        a1 != a2
        and (
            (a1 == at and other_side[b1:b2][: len(new)] == new)
            or (a2 == at and other_side[b1:b2][-len(new) :] == new)
        )
        for a1, a2, b1, b2 in others
    )


def _independent_edits[T](
    base: Sequence[T], ours: Sequence[T], theirs: Sequence[T]
) -> tuple[list[_Edit], list[_Edit]] | None:
    """Both sides' edits of ``base`` when none of them collide, else ``None``.

    An edit both sides made identically is ours alone. The line 3-way calls any
    two NEIGHBOURING edits a conflict; here only edits of the same place are.
    ``None`` as well when the two sides have too many edits to compare, or the
    differ gives up.
    """
    try:
        mine, other = _edits(base, ours), _edits(base, theirs)
    except (RecursionError, _MaxRecursionDepth):
        return None
    if len(mine) * len(other) > _MAX_EDIT_PAIRS:
        return None
    made = {edit[:2]: ours[edit[2] : edit[3]] for edit in mine}
    other = [edit for edit in other if made.get(edit[:2]) != theirs[edit[2] : edit[3]]]
    if any(_edits_collide(left, right) for left in mine for right in other):
        return None
    return mine, other


def _resolve_hunk(
    base: list[bytes], ours: list[bytes], theirs: list[bytes]
) -> list[bytes] | None:
    """The lines of one line-merge conflict hunk when the edits do not collide.

    Deterministic, with no trial: each side's edits of the hunk's base lines
    are applied together when no two of them touch the same line. Lines both
    sides inserted at the same spot come ours first, then those of theirs that
    ours did not insert as well, and an inserted line the other side's edit
    beside it already brings is not written again (:func:`_repeats`; for LINES
    only — in an array a repeated element is data). ``None`` when both sides
    changed the same line differently (a real collision) or the hunk is too
    large to check.
    """
    edits = _independent_edits(base, ours, theirs)
    if edits is None:
        return None
    mine = [edit for edit in edits[0] if not _repeats(edit, ours, edits[1], theirs)]
    other = [edit for edit in edits[1] if not _repeats(edit, theirs, edits[0], ours)]
    lines: list[bytes] = []
    cursor = 0
    inserted: tuple[int, list[bytes]] = (-1, [])
    for i1, i2, rank, new in sorted(
        [(i1, i2, 0, ours[j1:j2]) for i1, i2, j1, j2 in mine]
        + [(i1, i2, 1, theirs[j1:j2]) for i1, i2, j1, j2 in other]
    ):
        if rank and i1 == i2 == inserted[0]:
            new = [line for line in new if line not in inserted[1]]
        elif i1 == i2:
            inserted = (i1, new)
        lines += [*base[cursor:i1], *new]
        cursor = max(cursor, i2)
    return [*lines, *base[cursor:]]


def _line_merge(
    base: bytes, live: bytes, tracked: bytes, *, refine: bool
) -> bytes | None:
    """The line 3-way of the three texts, or ``None`` when it conflicts.

    With ``refine`` a conflict hunk whose edits do not collide
    (:func:`_resolve_hunk`) is resolved instead; any other hunk still makes the
    whole result ``None``.
    A missing final newline is not a difference between the sides: the last line
    gets a terminator for the merge, and the result ends the way live does.
    """
    sides = (base, live, tracked)
    # One terminator for all three: a side cut down to a single unterminated
    # line does not show which one the document uses.
    newline = b"\r\n" if any(b"\r\n" in side for side in sides) else b"\n"
    result = merge(*(_terminated(side, newline) for side in sides))
    parts: list[bytes] = []
    for segment in result.segments:
        if isinstance(segment, Clean):
            parts.append(segment.bytes_)
            continue
        hunk = (
            _resolve_hunk(
                split_lines(segment.base),
                split_lines(segment.ours),
                split_lines(segment.theirs),
            )
            if refine
            else None
        )
        if hunk is None:
            return None
        parts.extend(hunk)
    text = b"".join(parts)
    if live.endswith(b"\n") or not live:
        return text
    return text.removesuffix(b"\n").removesuffix(b"\r")


def _holds_merge(
    text: bytes, merged: object, fmt: StructuredFormat, *, commas: bool
) -> bool:
    """Whether ``text`` parses to exactly ``merged``'s values.

    Without ``commas`` a JSON text must not close an object or array after a
    comma either: joined lines can leave one that neither side wrote, which the
    json5 loader accepts and a strict consumer of the file rejects.
    """
    try:
        candidate = load_quietly(text, fmt)
        return models_equal(candidate, merged) and (
            commas or not _has_trailing_comma(candidate)
        )
    except (MergeTypeMismatch, DuplicateKeyInMergeModel, StructuredParseError):
        return False


def _has_trailing_comma(model: object) -> bool:
    """Whether a json-five model closes any object or array after a comma."""
    return any(
        getattr(node, "trailing_comma", None) is not None
        for node in json5_walk(cast("Node", model))
    )


def _merge_array_root(base: object, live: object, tracked: object) -> object | None:
    """Merge three JSON array-root models element-wise INTO ``live``, or ``None``.

    An object root has key identity; an array root has none, and its line 3-way
    false-conflicts whenever the last element gains a comma. Each element is one
    unit here. Elements the host inserted or removed are already in ``live``;
    upstream's edits are applied where they replace elements one for one, each
    new element taking the whitespace of the one it replaces. ``None`` when a
    root is not an array, when both sides edited the same place, and when
    upstream inserted or removed elements (no layout rule for those).
    """
    arrays = [getattr(model, "value", None) for model in (base, live, tracked)]
    if not all(isinstance(array, JSONArray) for array in arrays):
        return None
    base_keys, live_keys, tracked_keys = (
        [
            json.dumps(element, sort_keys=True)
            for element in cast("list[object]", get_at_path(array, ""))
        ]
        for array in arrays
    )
    edits = _independent_edits(base_keys, live_keys, tracked_keys)
    if edits is None:
        return None
    ours, theirs = edits
    if any(
        mine[0] == mine[1] == other[0] == other[1] for mine in ours for other in theirs
    ):
        return None  # both inserted at one point: the order of elements matters
    live_values = cast("JSONArray", arrays[1]).values
    tracked_values = cast("JSONArray", arrays[2]).values
    for i1, i2, j1, j2 in theirs:
        if i2 - i1 != j2 - j1 or i1 == i2:
            return None
        shift = sum((b - a) - (i2_ - i1_) for i1_, i2_, a, b in ours if i2_ <= i1)
        for offset in range(i2 - i1):
            old = live_values[i1 + shift + offset]
            new = tracked_values[j1 + offset]
            new.wsc_before, new.wsc_after = old.wsc_before, old.wsc_after
            live_values[i1 + shift + offset] = new
    return live


def _key_merge(
    base: bytes, live: bytes, tracked: bytes, fmt: StructuredFormat
) -> bytes | None:
    """The clean key-aware 3-way of three structured texts, or ``None``.

    ``None`` on a same-key conflict and for a side the key engine cannot model
    (unparseable, multi-document, duplicate or non-string keys, a non-mapping
    YAML root) — the caller line-merges those. Each side is parsed FRESH because
    the merge mutates the live model in place.

    The key merge decides the VALUES. The bytes are, in order: the line 3-way
    when it is clean; for YAML that line 3-way with the conflict hunks whose
    edits do not collide resolved (:func:`_resolve_hunk`); the re-serialised
    merged model. A line
    result counts only when it parses to exactly the merged values (and, for
    JSON, has no trailing comma neither side uses). There is no search: at most
    one candidate text is parsed.

    A YAML with aliases / merge keys shares one node between several keys, so
    its text is the truth: a clean line 3-way that parses is the result. Only
    when the lines conflict are the resolved values merged — on de-aliased plain
    copies, because merging in place would edit every key that shares a node —
    and the model is never re-serialised (the dump would inline the shared
    nodes and invent anchors).
    """
    yaml = fmt is StructuredFormat.YAML
    sides = (base, live, tracked)
    try:
        aliased = yaml and any(uses_aliases(side) for side in sides)
        models = [load_quietly(side, fmt) for side in sides]
        if aliased:
            text = _line_merge(*sides, refine=False)
            if text is not None and _parses(text, fmt):
                return text
            models = [get_at_path(model, "") for model in models]
        commas = yaml or any(_has_trailing_comma(model) for model in models[1:])
        merged = None if yaml else _merge_array_root(*models)
        if merged is None:
            result = merge_structural(*models)
            if not result.clean:
                return None
            merged = result.merged_model
        text = _line_merge(*sides, refine=yaml)
        if text is not None and _holds_merge(text, merged, fmt, commas=commas):
            return text
        if aliased:
            return None
        text = _dump_model(merged, fmt, like=live)
        if yaml and b"\n" not in live and any(b"\r\n" in side for side in sides):
            # A one-line live file shows no line ending for the dump to follow.
            text = text.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        return text
    except (MergeTypeMismatch, DuplicateKeyInMergeModel, StructuredParseError):
        return None


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
    line 3-way's whenever that holds the same values (see :func:`_key_merge` for
    how its conflict hunks are settled), else the re-serialised model. When one
    side did not move, the other side's bytes are used verbatim. The base-absent
    seed is
    byte-identical to the plain path. A GENUINE same-key collision
    (``merge_structural`` reports conflicts) is delegated to
    :func:`reconcile_plain_file`, so the one proven wizard / ``--auto`` / DEFERRED
    tail resolves it — no separate structured conflict UI is introduced.

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
