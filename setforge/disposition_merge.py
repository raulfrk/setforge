"""Non-interactive disposition merge driver.

Central resolution policy that maps a ``(disposition, auto, base-presence)``
triple to the bytes deployed at a live destination plus a re-baseline
decision. It is the single seam install uses to turn the stored-base 3-way
engines (:mod:`setforge.structural_merge`, :mod:`setforge.markdown_merge`)
into a concrete file outcome, free of any interactive prompting.

Orientation: ``ours = live``, ``theirs = tracked/upstream``. The two
``auto`` modes (reusing :class:`setforge.section_wizard.ReconcileAuto`) read
as ``keep-live = keep ours`` and ``use-tracked = take theirs``.

Three policy axes:

* **disposition** — :data:`~setforge.config.Disposition.PINNED` short-circuits
  to live verbatim (live is authoritative, base untouched);
  ``SHARED`` / ``FORKED`` both run the 3-way merge (the SHARED-vs-FORKED
  difference — whether live edits are captured back to tracked — is the
  caller's concern, not this driver's).
* **base presence** — a ``None`` base is a first run: a disposition file has
  no preserve config, so the fallback is to deploy ``tracked`` verbatim and
  seed the stored base to it.
* **auto + conflict** — a conflicting merge under a set ``auto`` resolves
  every conflict that way and re-baselines; under bare (``auto is None``) the
  conflict is left at ours (keep-live) and re-baselining is DEFERRED so the
  next run re-detects the still-pending divergence.

An OPTIONAL injectable ``resolver`` (a :data:`ConflictResolver`) lets an
interactive caller (a wizard, built elsewhere) drive each conflict
individually while this module stays pure — the I/O lives in the caller's
resolver, never here. When ``resolver is None`` resolution is byte-identical to
the ``auto`` policy above. When supplied, each conflict is routed through it
once, in document order, and its :class:`ConflictResolution` selects ours /
theirs / an edited payload / a skip. The re-baseline rule under a resolver is
ANY-SKIP-DEFERS: ``advance_base`` is ``True`` only when no conflict resolved to
:data:`ConflictChoice.SKIP` (a clean merge still advances); any skip leaves the
base untouched so the file re-detects the pending divergence next run, while the
conflicts list is still returned so the caller can report.

Format detection is by ``dst`` suffix: JSON/JSONC (``jsonc.is_jsonc_file``)
and ``.yaml`` / ``.yml`` route through the comment-preserving structural
engine; everything else (``.md``, arbitrary text) routes through the
line-based markdown engine. A structurally-incompatible structural file
(a :class:`~setforge.errors.MergeTypeMismatch` shape clash between sides)
falls back to the line-based path on the raw text — a malformed-shape file
still merges as text rather than crashing the install.
"""

import io
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from json5.dumper import ModelDumper
from json5.dumper import dumps as _json5_dumps
from json5.loader import ModelLoader
from json5.loader import loads as _json5_loads
from ruamel.yaml import YAML

from setforge import jsonc, yaml_merge
from setforge.config import Disposition
from setforge.errors import ConfigError, MergeTypeMismatch
from setforge.markdown_merge import (
    LineConflict,
    _split_strip_final,
    merge_markdown_segments,
    resolve_segments,
)
from setforge.scalar_merge import ABSENT, ScalarConflict
from setforge.section_wizard import ReconcileAuto
from setforge.spans import SpanEntry, SpanKind
from setforge.structural_merge import (
    PathConflict,
    delete_at_path,
    get_at_path,
    list_keys_at_path,
    merge_structural,
    set_at_path,
)

__all__ = [
    "ConflictChoice",
    "ConflictResolution",
    "ConflictResolver",
    "FileResolution",
    "StructuralSpanOrphan",
    "StructuralSpanOrphanReason",
    "exclude_structural_spans_for_capture",
    "is_structural",
    "resolve_file",
    "validate_structural_span_overlap",
    "validate_structural_spans",
]


class ConflictChoice(StrEnum):
    """A per-conflict decision an injected resolver returns.

    ``KEEP_OURS`` keeps the live side, ``TAKE_THEIRS`` takes the tracked side,
    ``EDIT`` substitutes a hand-edited payload (carried on
    :class:`ConflictResolution`), and ``SKIP`` leaves the conflict unresolved
    (ours is kept in the text, but the file is not re-baselined).
    """

    KEEP_OURS = "keep_ours"
    TAKE_THEIRS = "take_theirs"
    EDIT = "edit"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class ConflictResolution:
    """One resolver verdict for a single conflict.

    ``choice`` is the verdict. ``edited_lines`` carries the line-based EDIT
    payload (lines with terminators kept), consulted only for a line-based
    conflict resolved ``EDIT``. ``edited_value`` carries the structural EDIT
    payload (a plain scalar / list / dict), consulted only for a structural
    conflict resolved ``EDIT``. Both default to ``None`` for the non-EDIT
    choices.
    """

    choice: ConflictChoice
    edited_lines: list[str] | None = None
    edited_value: object | None = None


# A per-conflict resolver mapping one conflict to a verdict. Injected by an
# interactive caller (a wizard); the I/O lives in the caller, keeping this
# module pure. The conflict is a line-based ``LineConflict`` (line path), a
# structural ``PathConflict`` (structural path), or a scalar ``ScalarConflict``.
type ConflictResolver = Callable[
    [LineConflict | PathConflict | ScalarConflict], ConflictResolution
]


class StructuralSpanOrphanReason(StrEnum):
    """Why a structural span pin could not be re-asserted / located.

    ``ABSENT_IN_LIVE`` — the pinned path is gone from the FRESH live parse (the
    user deleted ``P`` locally); the snapshot is the ABSENT sentinel so there is
    nothing to re-impose (B-S4). ``MISSING_PARENT`` — an intermediate parent on
    the path is gone from the merged model (``set_at_path`` raised ``KeyError``
    / ``ValueError``); the seam the I9 key-identity orphan posture surfaces
    (B-S3). ``PARENT_NOT_MAPPING`` — the resolved parent is a scalar/list, not a
    mapping (``set_at_path`` raised ``MergeTypeMismatch``), so the leaf cannot be
    addressed by key (B-S3). ``UPSTREAM_RENAMED_OR_DELETED`` — refines
    ``ABSENT_IN_LIVE``: the path is gone from live AND the stored base HAD a
    value at ``P`` while tracked no longer does, so the loss is attributed to an
    upstream rename/delete rather than a local edit; the orphan carries the
    tracked-side sibling keys at ``P``'s parent so the warning can render a
    did-you-mean. The two ``set_at_path`` failure reasons are NOT refined —
    they name a more specific parent-level seam and their fixtures stay
    distinguishable (a parent-level upstream removal still reports WHERE the
    re-assert failed).
    """

    ABSENT_IN_LIVE = "absent-in-live"
    MISSING_PARENT = "missing-parent"
    PARENT_NOT_MAPPING = "parent-not-mapping"
    UPSTREAM_RENAMED_OR_DELETED = "upstream-renamed-or-deleted"


@dataclass(frozen=True, slots=True)
class StructuralSpanOrphan:
    """One structural span pin that could not be re-asserted onto the merge.

    ``anchor`` is the dotted path; ``kind`` records the span kind and is
    currently always :data:`~setforge.spans.SpanKind.PINNED` — only pinned spans
    re-assert, so only a pinned re-assert can orphan (forked spans never
    re-assert; their capture exclusion is silent). ``reason`` is the seam that
    failed. An orphan is
    PRESERVED (the merged value is left intact at ``P``) and warned, never
    dropped and never an uncaught raise (B-S3 / B-S4).

    ``tracked_siblings`` is populated ONLY for
    :data:`StructuralSpanOrphanReason.UPSTREAM_RENAMED_OR_DELETED`: the child
    key names at ``P``'s parent in the TRACKED model, in document order — the
    did-you-mean candidates the warning render site feeds to its close-match
    suggester. Empty for every other reason.
    """

    anchor: str
    kind: SpanKind
    reason: StructuralSpanOrphanReason
    tracked_siblings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FileResolution:
    """Outcome of :func:`resolve_file`.

    ``text`` is the bytes to write to the live destination. ``conflicts`` is
    empty on a clean merge and otherwise lists every conflicting hunk/path —
    it stays non-empty even when an ``auto`` mode resolved the conflict, so the
    caller can still warn. ``advance_base`` is ``True`` when the caller should
    re-baseline the stored base to ``text``; ``False`` means leave the base
    untouched — either a deferred bare conflict (re-detected next run) or a
    ``pinned`` file (which never re-baselines). ``base_absent`` is ``True``
    only on the first-run fallback (no stored base), where ``text == tracked``.

    ``structural_span_orphans`` lists every STRUCTURAL span pin whose path could
    not be re-asserted onto the merged model (absent in live, or a missing /
    non-mapping parent). It is always empty on the markdown / line-based path
    (markdown span orphans flow through the separate
    :mod:`setforge.spans_overlay` ladder). When non-empty the merged value is
    preserved at each orphaned path; the caller warns and never aborts (B-S3 /
    B-S4 / Invariant I6).
    """

    text: str
    conflicts: list[LineConflict | PathConflict]
    advance_base: bool
    base_absent: bool
    structural_span_orphans: list[StructuralSpanOrphan] = field(default_factory=list)


def resolve_file(
    disposition: Disposition,
    dst: Path,
    base: str | None,
    live: str,
    tracked: str,
    auto: ReconcileAuto | None,
    resolver: ConflictResolver | None = None,
    structural_spans: list[SpanEntry] | None = None,
    *,
    live_absent: bool = False,
) -> FileResolution:
    """Resolve one file to deployed text + a re-baseline decision.

    See the module docstring for the full policy. ``dst`` is used for format
    detection (suffix) only; no filesystem access occurs.

    ``resolver`` is an OPTIONAL per-conflict resolver an interactive caller (a
    wizard) injects. When ``resolver is None`` the resolution is byte-identical
    to the non-interactive ``auto`` policy. When ``resolver is not None`` and
    the merge conflicts, EACH conflict is driven through ``resolver`` once, in
    document order, instead of the blanket-``auto`` logic; the resolver's
    payload (:class:`ConflictResolution`) selects ours / theirs / an edited
    value / a skip. Re-baselining advances only when NO conflict was skipped
    (any :data:`ConflictChoice.SKIP` defers, so the file re-detects the still-
    pending divergence next run). ``resolver`` is irrelevant on the PINNED and
    base-absent paths (no merge runs there).

    ``structural_spans`` are the STRUCTURAL (dotted-path) span pins for this
    file. They are honored only on the structural merge path: each PINNED span's
    live value at ``P`` is snapshotted BEFORE the merge and re-asserted onto the
    merged model AFTER it (so an upstream-changed-but-live-unchanged ``P`` is
    not silently taken toward tracked); the re-baseline dump is taken AFTER the
    re-assert (B-S6). FORKED spans are not re-asserted (capture exclusion only).
    Spans are ignored on the PINNED / base-absent / line-based paths.

    ``live_absent`` is True when the destination file did not exist — the
    caller passes ``live=""`` as a placeholder in that case. Consumed ONLY
    by the PINNED branch (the first install deploys tracked instead of the
    empty placeholder); every other path already handles first-run state
    via ``base is None``.
    """
    if disposition is Disposition.PINNED:
        if live_absent:
            # Fresh host: there is no live file for "never overwrite live"
            # to keep, so the FIRST install deploys the tracked bytes;
            # every later run sees a live file and returns it untouched.
            return FileResolution(
                text=tracked, conflicts=[], advance_base=False, base_absent=False
            )
        return FileResolution(
            text=live, conflicts=[], advance_base=False, base_absent=False
        )

    if base is None:
        # First run: a disposition file has no preserve config, so the 2-way
        # fallback deploys tracked verbatim and seeds base = text = tracked.
        return FileResolution(
            text=tracked, conflicts=[], advance_base=True, base_absent=True
        )

    if is_structural(dst):
        try:
            return _resolve_structural(
                dst, base, live, tracked, auto, resolver, structural_spans
            )
        except MergeTypeMismatch:
            # A structurally-incompatible file (shape clash between sides)
            # still merges as raw text via the line-based path.
            return _resolve_line_based(base, live, tracked, auto, resolver)

    return _resolve_line_based(base, live, tracked, auto, resolver)


def is_structural(dst: Path) -> bool:
    """Whether ``dst`` routes through the structural (comment-tree) engine.

    Public seam: the install (``deploy``) and capture paths dispatch span
    handling on this predicate, so it is part of the module's surface rather
    than a private helper.
    """
    return jsonc.is_jsonc_file(dst) or dst.suffix in {".yaml", ".yml"}


# ---------------------------------------------------------------------------
# Structural (YAML / JSONC) path.
# ---------------------------------------------------------------------------


def _resolve_structural(
    dst: Path,
    base: str,
    live: str,
    tracked: str,
    auto: ReconcileAuto | None,
    resolver: ConflictResolver | None = None,
    structural_spans: list[SpanEntry] | None = None,
) -> FileResolution:
    """Run the comment-preserving 3-way merge over ``dst``'s structural model.

    Each side is parsed FRESH (never aliased) because
    :func:`setforge.structural_merge.merge_structural` mutates ours in place.
    The merged model is ours mutated, bearing ours' value at every
    :class:`~setforge.structural_merge.PathConflict`.

    With ``resolver is None`` conflicts resolve per ``auto`` (the historical
    path). With a ``resolver`` each :class:`~setforge.structural_merge.PathConflict`
    is driven through it once, in document order: ``KEEP_OURS`` / ``SKIP`` leave
    ours (already in the model), ``TAKE_THEIRS`` writes theirs, ``EDIT`` writes
    the resolution's ``edited_value``. Any ``SKIP`` defers re-baselining.

    STRUCTURAL span pins (``structural_spans``) re-impose live over the merge:
    each PINNED span's live value at ``P`` is SNAPSHOTTED as an unwrapped plain
    value from the FRESH ``live_model`` BEFORE the merge mutates it (B-S1 / B-S2),
    then re-asserted via :func:`~setforge.structural_merge.set_at_path` AFTER the
    merge (B-S3). The re-baseline dump (``text``) is taken AFTER the re-assert so
    base == what landed live (B-S6). FORKED spans are NOT re-asserted. Each
    re-assert is wrapped so a missing parent (``KeyError`` / ``ValueError``) or a
    non-mapping parent (``MergeTypeMismatch``) orphan-warns + skips instead of
    aborting the install.
    """
    pinned = _pinned_structural_spans(structural_spans)

    is_jsonc = jsonc.is_jsonc_file(dst)
    base_model = _load_structural(base, is_jsonc)
    live_model = _load_structural(live, is_jsonc)
    tracked_model = _load_structural(tracked, is_jsonc)

    # SNAPSHOT each pinned path's live value BEFORE the merge mutates live_model
    # in place — as an unwrapped, deep-copied plain value (B-S1 / B-S2). ABSENT
    # marks a path the user deleted locally (B-S4).
    live_snapshots: dict[str, object] = {
        span.anchor: get_at_path(live_model, span.anchor) for span in pinned
    }
    # CLASSIFY upstream-gone paths BEFORE the merge too (the base / tracked
    # models are parsed exactly once, above): when the stored base HAD a value
    # at P and tracked no longer does, a live-absent P is an upstream
    # rename/delete, and the tracked siblings at P's parent are the
    # did-you-mean candidates.
    upstream_gone: dict[str, tuple[str, ...] | None] = {
        span.anchor: _upstream_gone_siblings(base_model, tracked_model, span.anchor)
        for span in pinned
    }

    result = merge_structural(base_model, live_model, tracked_model)
    conflicts: list[LineConflict | PathConflict] = list(result.conflicts)

    if not result.conflicts:
        advance = True
    elif resolver is not None:
        advance = _apply_structural_resolver(
            result.merged_model, result.conflicts, resolver
        )
    elif auto is ReconcileAuto.USE_TRACKED:
        for pc in result.conflicts:
            set_at_path(result.merged_model, pc.path, pc.theirs)
        advance = True
    else:
        # KEEP_LIVE or bare: ours is already in the model; nothing to write.
        advance = auto is ReconcileAuto.KEEP_LIVE

    # RE-ASSERT pinned spans AFTER the merge / conflict resolution, so the live
    # value wins over an upstream auto-resolved-toward-theirs ``P`` (the whole
    # point of a pin). Orphans are preserved + reported, never raised.
    orphans = _reassert_pinned_spans(
        result.merged_model, pinned, live_snapshots, upstream_gone
    )

    # SUPPRESS pinned-path conflicts (B-S6 / I1): a pin is deterministic
    # live-wins, so a ``PathConflict`` whose path is a pinned span is NOT a real
    # deferrable conflict — the re-assert above already imposed live there. If
    # such a conflict were left in the list, the caller would warn ("kept live,
    # base not advanced") and ``advance_base`` would follow the bare-defer rule,
    # so the base never advances and the phantom conflict re-surfaces every
    # install. We strip exactly the pinned-path conflicts (NOT a non-pinned
    # conflict, which has no override and must still defer/advance per the
    # normal rule) and, if every suppressed conflict was a pin, force the base
    # to advance.
    pinned_anchors = {span.anchor for span in pinned}
    suppressed_any = any(
        isinstance(c, PathConflict) and c.path in pinned_anchors for c in conflicts
    )
    conflicts = [
        c
        for c in conflicts
        if not (isinstance(c, PathConflict) and c.path in pinned_anchors)
    ]
    if suppressed_any and not conflicts:
        # The only conflicts were pinned-path ones: there is nothing left to
        # defer, so re-baseline to the post-reassert dump (live-wins is final).
        advance = True

    # Re-baseline dump is taken AFTER the re-assert (B-S6).
    return FileResolution(
        text=_dump_structural(result.merged_model, is_jsonc),
        conflicts=conflicts,
        advance_base=advance,
        base_absent=False,
        structural_span_orphans=orphans,
    )


def _pinned_structural_spans(
    structural_spans: list[SpanEntry] | None,
) -> list[SpanEntry]:
    """Return the PINNED structural spans in deterministic apply order (I11).

    Forked spans never re-assert, so they are dropped here. The remaining pins
    are validated pairwise non-overlapping (:func:`validate_structural_span_overlap`)
    and returned sorted by anchor so the apply order is deterministic (B-S7).
    """
    if not structural_spans:
        return []
    validate_structural_spans(structural_spans)
    pinned = [s for s in structural_spans if s.kind is SpanKind.PINNED]
    return sorted(pinned, key=lambda s: s.anchor)


def validate_structural_spans(spans: list[SpanEntry]) -> None:
    """Run the structural-span integrity checks (list-index + overlap).

    The single offline-validate seam over a tracked_file's STRUCTURAL spans:
    rejects a list-index anchor (Invariant I10) and any overlapping / nested
    pins (Invariant I11), the same two guards the install-time merge enforces
    via :func:`_pinned_structural_spans`. Surfacing them here lets
    ``setforge validate`` (the offline CI gate) catch a malformed structural
    span declaration BEFORE install would abort mid-deploy with a
    :class:`~setforge.errors.ConfigError`.
    """
    _reject_list_index_anchors(spans)
    validate_structural_span_overlap(spans)


def _reject_list_index_anchors(spans: list[SpanEntry]) -> None:
    """Reject any list-suffix span anchor at pin time (Invariant I10).

    :func:`~setforge.structural_merge.set_at_path` addresses MAPPING leaves
    only; a list-element-by-index pin (``a[*]`` / ``a[]``) has no stable key
    identity across an upstream reorder, so it is refused up front with a clear
    :class:`~setforge.errors.ConfigError` rather than failing opaquely at the
    get / set seam.
    """
    for span in spans:
        if "[*]" in span.anchor or "[]" in span.anchor:
            raise ConfigError(
                f"structural span anchor {span.anchor!r} uses a list suffix; "
                "list-element pins are not supported (anchors must address a "
                "mapping leaf or whole subtree by key)."
            )


def _upstream_gone_siblings(
    base_model: object, tracked_model: object, anchor: str
) -> tuple[str, ...] | None:
    """Classify ``anchor`` as upstream-gone and return its sibling candidates.

    Returns ``None`` when the path was NOT dropped upstream — the stored base
    never had a value at ``anchor`` (nothing upstream to lose) or tracked
    still has one. Otherwise returns the child key names at ``anchor``'s
    parent in the TRACKED model (document order; the root keys for a
    top-level anchor) — the did-you-mean candidates carried on the orphan.
    The tuple may be empty (the parent is itself gone or empty upstream); an
    empty candidate set still classifies as upstream-gone, it just yields no
    suggestion.
    """
    if get_at_path(base_model, anchor) is ABSENT:
        return None
    if get_at_path(tracked_model, anchor) is not ABSENT:
        return None
    parent, _, _leaf = anchor.rpartition(".")
    return tuple(list_keys_at_path(tracked_model, parent))


def _reassert_pinned_spans(
    model: object,
    pinned: list[SpanEntry],
    live_snapshots: dict[str, object],
    upstream_gone: dict[str, tuple[str, ...] | None],
) -> list[StructuralSpanOrphan]:
    """Re-impose each pinned span's snapshotted live value onto ``model``.

    Returns the orphan list (paths that could not be re-asserted). An ABSENT
    snapshot (the user deleted ``P`` locally) skips-with-warn (B-S4) — unless
    ``upstream_gone`` marks the path as dropped upstream (base had a value,
    tracked no longer does), in which case the orphan classifies as
    :data:`StructuralSpanOrphanReason.UPSTREAM_RENAMED_OR_DELETED` carrying
    the tracked sibling candidates. A
    ``KeyError`` / ``ValueError`` (missing parent) or a
    ``MergeTypeMismatch`` (non-mapping parent) from
    :func:`~setforge.structural_merge.set_at_path` orphan-warns + skips, never
    an uncaught raise (B-S3).

    A span with ``deep=True`` DEEP-merges its snapshot over the merged value
    (tracked-only sub-keys survive) instead of whole-replacing — the schema
    2.0 carrier for the legacy ``preserve_user_keys_deep`` semantics. The
    same orphan postures apply on the deep path.
    """
    orphans: list[StructuralSpanOrphan] = []
    for span in pinned:
        snapshot = live_snapshots[span.anchor]
        if snapshot is ABSENT:
            siblings = upstream_gone[span.anchor]
            if siblings is not None:
                orphans.append(
                    StructuralSpanOrphan(
                        anchor=span.anchor,
                        kind=span.kind,
                        reason=(StructuralSpanOrphanReason.UPSTREAM_RENAMED_OR_DELETED),
                        tracked_siblings=siblings,
                    )
                )
            else:
                orphans.append(
                    StructuralSpanOrphan(
                        anchor=span.anchor,
                        kind=span.kind,
                        reason=StructuralSpanOrphanReason.ABSENT_IN_LIVE,
                    )
                )
            continue
        try:
            if span.deep:
                _deep_reassert_span(model, span.anchor, snapshot)
            else:
                set_at_path(model, span.anchor, snapshot)
        except (KeyError, ValueError):
            orphans.append(
                StructuralSpanOrphan(
                    anchor=span.anchor,
                    kind=span.kind,
                    reason=StructuralSpanOrphanReason.MISSING_PARENT,
                )
            )
        except MergeTypeMismatch:
            orphans.append(
                StructuralSpanOrphan(
                    anchor=span.anchor,
                    kind=span.kind,
                    reason=StructuralSpanOrphanReason.PARENT_NOT_MAPPING,
                )
            )
    return orphans


def _deep_reassert_span(model: object, anchor: str, snapshot: object) -> None:
    """Deep-merge ``snapshot`` (live) over the merged value at ``anchor``.

    The schema 2.0 carrier of the legacy ``preserve_user_keys_deep`` semantics:
    rather than whole-replacing the merged subtree with live's value (the
    shallow PINNED re-assert), deep-merge live OVER the merged value so a
    tracked-only sub-key the 3-way merge kept survives, while live's edited
    sub-keys win.

    Both sides are unwrapped plain values — :func:`get_at_path` returns the
    merged value already unwrapped, and ``snapshot`` is the unwrapped live
    snapshot — so the deep merge runs on backend-agnostic python structures via
    :func:`setforge.yaml_merge._deep_merge_dicts`; the result is written back
    through :func:`~setforge.structural_merge.set_at_path` (the same comment-
    preserving seam the shallow re-assert uses). When either side is not a
    mapping the deep merge is degenerate, so live whole-replaces (set the
    snapshot) — matching the legacy overlay's scalar/list terminal.

    Raises the same ``KeyError`` / ``ValueError`` / ``MergeTypeMismatch`` the
    shallow path raises on a missing / non-mapping parent, so the caller's
    orphan postures cover the deep path too.
    """
    merged_value = get_at_path(model, anchor)
    if isinstance(merged_value, MutableMapping) and isinstance(snapshot, Mapping):
        # Deep-merge live's snapshot over the merged subtree in place, then
        # write the merged result back at the anchor.
        yaml_merge._deep_merge_dicts(merged_value, snapshot, anchor)
        set_at_path(model, anchor, merged_value)
        return
    # Degenerate (scalar / list / absent terminal): live whole-replaces.
    set_at_path(model, anchor, snapshot)


def validate_structural_span_overlap(spans: list[SpanEntry]) -> None:
    """Reject overlapping / nested structural span pins (Invariant I11, B-S7).

    Two dotted-path anchors overlap when one is a prefix of the other (so a
    whole-subtree :func:`~setforge.structural_merge.set_at_path` at the ancestor
    would clobber the descendant pin's value, a last-writer-wins hazard). The
    ONLY legal nesting in the file+span model is a pinned subtree containing a
    forked leaf; that is out of scope for the dotted-path engine, so any prefix
    overlap (including duplicate anchors) is refused with :class:`ConfigError`.
    """
    seen: list[str] = []
    for span in spans:
        anchor = span.anchor
        for other in seen:
            if _paths_overlap(anchor, other):
                raise ConfigError(
                    "overlapping / nested structural span anchors are not "
                    f"allowed: {other!r} and {anchor!r} (one is a prefix of the "
                    "other). Pin disjoint paths, or pin the common ancestor only."
                )
        seen.append(anchor)


def _paths_overlap(a: str, b: str) -> bool:
    """Whether dotted paths ``a`` / ``b`` are equal or one prefixes the other.

    Prefix is matched at SEGMENT granularity (``a`` prefixes ``a.b`` but ``a``
    does NOT prefix ``ab``), so sibling keys sharing a string prefix do not
    spuriously collide.
    """
    if a == b:
        return True
    sa = a.split(".")
    sb = b.split(".")
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return longer[: len(shorter)] == shorter


def exclude_structural_spans_for_capture(
    live_text: str,
    tracked_text: str,
    spans: list[SpanEntry],
    is_jsonc: bool,
) -> tuple[str, list[str]]:
    """Return capture text + warnings: live with every span path kept tracked.

    The structural sibling of
    :func:`setforge.spans_overlay.exclude_spans_for_capture`. Capture exclusion
    is TOTAL (Invariant I2): BOTH pinned AND forked span paths are restored to
    tracked's value in a live→tracked writeback, so a host-local span value
    never bakes into the shared config repo (B-S5). The rest of the live file
    captures normally.

    A span whose path is absent in tracked has nothing to restore — the live
    value at P is DELETED from the capture text (it is host-local by span
    intent and must not bake into the repo) and a warning is returned so the
    sync surface can report the uncaptured host value. When live ALSO lacks P
    the span is a converged no-op: nothing dropped, no warning. A path whose
    parent is missing / non-mapping in live is silently skipped — capture
    never aborts (the orphan is surfaced loudly by the install path, not
    here).
    """
    if not spans:
        return live_text, []
    live_model = _load_structural(live_text, is_jsonc)
    tracked_model = _load_structural(tracked_text, is_jsonc)
    warnings: list[str] = []
    for span in spans:
        tracked_value = get_at_path(tracked_model, span.anchor)
        if tracked_value is ABSENT:
            # Tracked has no value at P — drop live's host value (if any)
            # from the writeback rather than baking it into the repo.
            if get_at_path(live_model, span.anchor) is ABSENT:
                continue
            delete_at_path(live_model, span.anchor)
            warnings.append(
                f"span path {span.anchor} absent in tracked — host value not captured"
            )
            continue
        try:
            set_at_path(live_model, span.anchor, tracked_value)
        except (KeyError, ValueError, MergeTypeMismatch):
            # P's parent missing / non-mapping in live: skip silently; the
            # install path reports the orphan loudly.
            continue
    return _dump_structural(live_model, is_jsonc), warnings


def _apply_structural_resolver(
    model: object,
    conflicts: list[PathConflict],
    resolver: ConflictResolver,
) -> bool:
    """Drive each ``PathConflict`` through ``resolver``; return ``advance_base``.

    Calls ``resolver`` exactly once per conflict, in document order, applying
    its verdict into ``model`` (ours already lives there). Returns ``True`` only
    when no conflict was skipped (any ``SKIP`` defers re-baselining).
    """
    any_skip = False
    for pc in conflicts:
        res = resolver(pc)
        match res.choice:
            case ConflictChoice.KEEP_OURS:
                pass  # ours already in the model.
            case ConflictChoice.TAKE_THEIRS:
                set_at_path(model, pc.path, pc.theirs)
            case ConflictChoice.EDIT:
                set_at_path(model, pc.path, res.edited_value)
            case ConflictChoice.SKIP:
                any_skip = True
    return not any_skip


def _load_structural(text: str, is_jsonc: bool) -> object:
    """Parse ``text`` into a fresh comment-preserving model.

    JSONC goes through json-five's :class:`~json5.loader.ModelLoader`
    (comments / formatting on ``.wsc_before`` / ``.wsc_after``); YAML through
    ruamel ``YAML(typ="rt")`` round-trip mode (comments / anchors / quotes
    preserved), matching :func:`setforge.deploy._render_with_preserve_keys`.
    """
    if is_jsonc:
        return _json5_loads(text, loader=ModelLoader())
    yaml = _rt_yaml()
    return yaml.load(io.StringIO(text))


def _dump_structural(model: object, is_jsonc: bool) -> str:
    """Serialize a merged structural ``model`` back to byte-faithful text.

    Mirrors the load idioms in :func:`_load_structural` /
    :func:`setforge.deploy._render_with_preserve_keys`: json-five's
    :class:`~json5.dumper.ModelDumper` and ruamel ``YAML(typ="rt")`` keep
    comments, anchors, quote styles and key order intact on round-trip.
    """
    if is_jsonc:
        return _json5_dumps(model, dumper=ModelDumper())
    yaml = _rt_yaml()
    buf = io.StringIO()
    yaml.dump(model, buf)
    return buf.getvalue()


def _rt_yaml() -> YAML:
    """Build a ruamel round-trip YAML configured for byte-faithful preserve.

    ``preserve_quotes`` keeps a scalar's original quote style across the
    round-trip, matching the project's preserve idiom.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    return yaml


# ---------------------------------------------------------------------------
# Line-based (markdown / arbitrary text) path.
# ---------------------------------------------------------------------------


def _resolve_line_based(
    base: str,
    live: str,
    tracked: str,
    auto: ReconcileAuto | None,
    resolver: ConflictResolver | None = None,
) -> FileResolution:
    """Run the line-based 3-way merge over the raw text triple.

    With ``resolver is None`` conflicts resolve per ``auto``: ``USE_TRACKED``
    chooses theirs, every other mode (``KEEP_LIVE`` and bare) chooses ours;
    re-baselining advances on a clean merge or any set ``auto``, a bare conflict
    defers. With a ``resolver`` each :class:`~setforge.markdown_merge.LineConflict`
    is driven through it once, in document order: ``KEEP_OURS`` / ``SKIP`` keep
    ours, ``TAKE_THEIRS`` takes theirs, ``EDIT`` splices the resolution's
    ``edited_lines``. Any ``SKIP`` defers re-baselining.
    """
    segments = merge_markdown_segments(base, live, tracked)
    conflicts: list[LineConflict | PathConflict] = [
        seg for seg in segments if isinstance(seg, LineConflict)
    ]
    _ours_lines, ours_terminator = _split_strip_final(live)

    if resolver is not None:
        resolver_choose, any_skip_flag = _make_resolver_choose(resolver)
        text = resolve_segments(segments, resolver_choose, ours_terminator)
        # Any skip defers; a clean merge (no conflicts) still advances.
        advance = not any_skip_flag[0]
        return FileResolution(
            text=text,
            conflicts=conflicts,
            advance_base=advance,
            base_absent=False,
        )

    def _take_theirs(conflict: LineConflict) -> list[str]:
        return conflict.theirs

    def _take_ours(conflict: LineConflict) -> list[str]:
        return conflict.ours

    choose: Callable[[LineConflict], list[str]] = (
        _take_theirs if auto is ReconcileAuto.USE_TRACKED else _take_ours
    )

    text = resolve_segments(segments, choose, ours_terminator)

    # Clean -> advance; conflict under a set auto -> advance; bare conflict -> defer.
    advance = True if not conflicts else auto is not None

    return FileResolution(
        text=text,
        conflicts=conflicts,
        advance_base=advance,
        base_absent=False,
    )


def _make_resolver_choose(
    resolver: ConflictResolver,
) -> tuple[Callable[[LineConflict], list[str]], list[bool]]:
    """Build the ``resolve_segments`` chooser that drives ``resolver``.

    Returns the chooser plus a single-element ``[bool]`` cell that the chooser
    sets to ``True`` if any conflict resolves to ``SKIP`` (a mutable cell rather
    than a closed-over scalar so the flag survives the callback). The chooser is
    invoked once per :class:`~setforge.markdown_merge.LineConflict` in document
    order by :func:`~setforge.markdown_merge.resolve_segments`.
    """
    any_skip: list[bool] = [False]

    def _choose(conflict: LineConflict) -> list[str]:
        res = resolver(conflict)
        match res.choice:
            case ConflictChoice.KEEP_OURS:
                return conflict.ours
            case ConflictChoice.TAKE_THEIRS:
                return conflict.theirs
            case ConflictChoice.EDIT:
                if res.edited_lines is None:
                    raise ValueError(
                        "ConflictResolution(choice=EDIT) requires edited_lines"
                    )
                return res.edited_lines
            case ConflictChoice.SKIP:
                any_skip[0] = True
                return conflict.ours

    return _choose, any_skip
