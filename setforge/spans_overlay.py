"""Span merge re-overlay (Stage 5 of the span engine).

Runs AFTER the whole-file merge (:func:`setforge.disposition_merge.resolve_file`),
never threaded into the merge internals. For each PINNED span it splices
the live bytes over the merged region (live wins, Invariant I3 — applied
unconditionally as a post-merge override). FORKED spans get NO merge
override (the merge result is kept) but are still recomputed so capture
can exclude them. The recomputed per-span state is derived from the
POST-splice text — the bytes that actually land on disk — so the caller
re-baselines the byte base to those bytes, not the pre-splice merge result
(Invariant I1).

Orphan posture (Invariant I7): a span whose anchor cannot be relocated in
the merged text is PRESERVED (the merged text is left intact for that
region) and reported as an :class:`SpanOrphan`; it is never silently
dropped and never aborts the install (the caller decides whether
``--strict-spans`` escalates a pinned orphan to a refusal).

Splice order (Invariant I11): pinned splices are applied bottom-up by
line offset so an earlier splice never invalidates a later span's
offsets; pinned applies after forked (forked performs no text edit, so
order between the two is immaterial to the bytes).
"""

import hashlib
from dataclasses import dataclass, field

from setforge.errors import AnchorAmbiguousError, AnchorNotFoundError
from setforge.markdown_spans import bound_span, heading_level
from setforge.spans import SpanEntry, SpanKind
from setforge.spans_relocation import RelocationStatus, relocate_span
from setforge.spans_store import SpanState

__all__ = [
    "SpanOrphan",
    "SpanOverlayResult",
    "apply_spans",
    "exclude_spans_for_capture",
]

_CONTEXT_LINES = 3


@dataclass(slots=True, frozen=True)
class SpanOrphan:
    """One span that could not be relocated in the merged text.

    ``anchor`` identifies the span; ``kind`` distinguishes a pinned orphan
    (which ``--strict-spans`` may escalate) from a forked one. Both are
    preserved + warned, never dropped (Invariant I7).

    ``reason`` and ``tracked_siblings`` ride along ONLY for STRUCTURAL
    orphans (the deploy seam copies them from
    ``setforge.disposition_merge.StructuralSpanOrphan`` — ``reason`` holds
    that enum's value as a plain string so this markdown-side module never
    imports the merge driver). The warning render site uses them to
    attribute an upstream rename/delete and offer a did-you-mean over the
    tracked sibling keys. Markdown orphans leave both at their defaults.
    """

    anchor: str
    kind: SpanKind
    reason: str | None = None
    tracked_siblings: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SpanOverlayResult:
    """Outcome of :func:`apply_spans`.

    ``text`` is the post-splice bytes to write live AND re-baseline the
    byte base to (Invariant I1). ``new_states`` maps each successfully
    located span anchor to its recomputed :class:`SpanState` (for the
    spans sidecar). ``orphans`` lists every span whose anchor went missing
    upstream — preserved + warned, never dropped.
    """

    text: str
    new_states: dict[str, SpanState]
    orphans: list[SpanOrphan] = field(default_factory=list)


def _fingerprint(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _state_from_span(
    anchor: str, text: str, start_line: int, end_line: int, level: int
) -> SpanState:
    """Build a fresh :class:`SpanState` from a located region in ``text``."""
    lines = text.splitlines()
    body = "".join(text.splitlines(keepends=True)[start_line:end_line])
    return SpanState(
        anchor=anchor,
        fingerprint=_fingerprint(body),
        prefix=lines[max(0, start_line - _CONTEXT_LINES) : start_line],
        suffix=lines[end_line : end_line + _CONTEXT_LINES],
        position_hint_start_line=start_line,
        position_hint_n_lines=end_line - start_line,
        heading_level=level,
    )


@dataclass(slots=True, frozen=True)
class _Splice:
    """A pending pinned-span splice over the merged text."""

    start_line: int
    end_line: int
    replacement: str


def apply_spans(
    merged_text: str,
    live_text: str,
    spans: list[SpanEntry],
    stored_states: dict[str, SpanState],
) -> SpanOverlayResult:
    """Re-overlay ``spans`` onto ``merged_text`` after a whole-file merge.

    ``merged_text`` is the whole-file merge result; ``live_text`` is the
    current on-disk bytes (the pinned override source); ``stored_states``
    is the spans sidecar manifest keyed by anchor. Returns a
    :class:`SpanOverlayResult` whose ``text`` is the post-splice bytes,
    ``new_states`` the recomputed sidecar entries, and ``orphans`` the
    unrelocatable spans (preserved + reported).

    No-op (identity) when ``spans`` is empty. See the module docstring for
    the override / orphan / splice-order policy.
    """
    if not spans:
        return SpanOverlayResult(text=merged_text, new_states={}, orphans=[])

    new_states: dict[str, SpanState] = {}
    orphans: list[SpanOrphan] = []
    splices: list[_Splice] = []

    for span in spans:
        stored = stored_states.get(span.anchor)
        merged_loc = _relocate(merged_text, span.anchor, stored)
        if merged_loc is None:
            # Anchor gone from the merged text: preserve + warn (I7).
            orphans.append(SpanOrphan(anchor=span.anchor, kind=span.kind))
            continue

        if span.kind is SpanKind.PINNED and live_text:
            # ``live_text`` empty == first install / live file absent:
            # there is nothing to override, the merged text already equals
            # tracked, and the span state is simply seeded from it below —
            # NOT an orphan. A non-empty live whose region is gone IS a
            # user-side deletion (handled as an orphan).
            live_loc = _relocate(live_text, span.anchor, stored)
            if live_loc is None:
                # The user deleted the pinned region locally. Skip the
                # override (nothing live to impose) and orphan-warn so the
                # state is not silently re-baselined to the merged body.
                orphans.append(SpanOrphan(anchor=span.anchor, kind=span.kind))
                continue
            live_lines = live_text.splitlines(keepends=True)
            replacement = "".join(live_lines[live_loc[0] : live_loc[1]])
            splices.append(
                _Splice(
                    start_line=merged_loc[0],
                    end_line=merged_loc[1],
                    replacement=replacement,
                )
            )
        # FORKED: no splice; state is recomputed from the merged region
        # below after all splices are applied.

    post = _apply_splices(merged_text, splices)

    # Recompute every located span's state from the POST-splice text so
    # the sidecar (and the re-baselined byte base) reflect what landed.
    # This re-resolves the ladder a second time intentionally: a pinned
    # splice replaces the merged region with live bytes, so the located
    # offsets / fingerprint can differ from the pre-splice pass and MUST
    # be read from the bytes that actually hit disk (Invariant I1).
    orphan_anchors = {o.anchor for o in orphans}
    for span in spans:
        if span.anchor in orphan_anchors:
            continue
        loc = _relocate(post, span.anchor, stored_states.get(span.anchor))
        if loc is None:
            orphans.append(SpanOrphan(anchor=span.anchor, kind=span.kind))
            continue
        new_states[span.anchor] = _state_from_span(
            span.anchor, post, loc[0], loc[1], _level_at(post, loc[0])
        )

    return SpanOverlayResult(text=post, new_states=new_states, orphans=orphans)


def exclude_spans_for_capture(
    live_text: str,
    tracked_text: str,
    spans: list[SpanEntry],
    stored_states: dict[str, SpanState],
) -> str:
    """Return the capture text: live with every span region kept as tracked.

    Capture exclusion is TOTAL (Invariant I2): BOTH pinned AND forked span
    regions are excluded from a live→tracked writeback, so a host-local
    span body never bakes into the shared config repo. This reuses the
    "keep tracked over live for these regions" splice pattern — the live
    bytes are written back EXCEPT inside each span, where the existing
    tracked bytes are preserved.

    A span whose anchor cannot be relocated in BOTH live and tracked is
    silently left alone (the live bytes flow through for that region) —
    capture never aborts, and the orphan is surfaced loudly by the install
    path, not here.
    """
    if not spans:
        return live_text
    splices: list[_Splice] = []
    tracked_lines = tracked_text.splitlines(keepends=True)
    for span in spans:
        stored = stored_states.get(span.anchor)
        live_loc = _relocate(live_text, span.anchor, stored)
        tracked_loc = _relocate(tracked_text, span.anchor, stored)
        if live_loc is None or tracked_loc is None:
            continue
        replacement = "".join(tracked_lines[tracked_loc[0] : tracked_loc[1]])
        splices.append(
            _Splice(
                start_line=live_loc[0],
                end_line=live_loc[1],
                replacement=replacement,
            )
        )
    return _apply_splices(live_text, splices)


def _relocate(
    text: str, anchor: str, stored: SpanState | None
) -> tuple[int, int] | None:
    """Return the ``(start_line, end_line)`` of ``anchor`` in ``text``, or None.

    Uses the full relocation ladder when a stored state exists; otherwise
    falls back to a direct heading bound (first install, no manifest yet).
    Any ambiguity / absence collapses to ``None`` (orphan) — never a crash.
    """
    if stored is not None:
        result = relocate_span(text, anchor, stored)
        if result.status is RelocationStatus.LOCATED and result.span is not None:
            return (result.span.start_line, result.span.end_line)
        return None
    try:
        span = bound_span(text, anchor)
    except (AnchorAmbiguousError, AnchorNotFoundError):
        return None
    return (span.start_line, span.end_line)


def _level_at(text: str, start_line: int) -> int:
    """Return the ATX heading level at ``start_line`` (1 if not a heading)."""
    lines = text.splitlines()
    if 0 <= start_line < len(lines):
        level = heading_level(lines[start_line])
        if level is not None:
            return level
    return 1


def _apply_splices(text: str, splices: list[_Splice]) -> str:
    """Apply ``splices`` to ``text`` bottom-up so offsets stay valid.

    Pinned splices are non-overlapping (Invariant I11), so a stable
    descending sort by ``start_line`` lets each splice edit the line list
    without shifting the indices of splices yet to apply.
    """
    if not splices:
        return text
    lines = text.splitlines(keepends=True)
    for splice in sorted(splices, key=lambda s: s.start_line, reverse=True):
        replacement_lines = splice.replacement.splitlines(keepends=True)
        lines[splice.start_line : splice.end_line] = replacement_lines
    return "".join(lines)
