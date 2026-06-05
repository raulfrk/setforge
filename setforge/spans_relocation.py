"""Markdown span relocation ladder (Stage 4 of the span engine).

Locates a span in the CURRENT file content given its stored
:class:`~setforge.spans_store.SpanState`. The ladder is first-hit-wins,
each stage a hint not a pointer (line numbers are a search-start hint,
never the location key):

1. **fingerprint exact-match at position_hint** — the fast path: bound a
   span at the heading nearest the stored hint and accept it if its body
   fingerprint matches.
2. **fingerprint unique-anywhere** — accept the unique heading whose
   bounded body fingerprint matches; MULTIPLE identical-fingerprint hits
   orphan (never pick-first, Invariant I8).
3. **heading resolve** — body edited (no fingerprint hit) but the heading
   text+level is still present and UNIQUE; a duplicate heading orphans.
4. **diff-match-patch fuzzy** — ``match_main`` with conservative
   thresholds biased to orphan; a low-confidence match orphans rather
   than risk a confident wrong-relocation (as harmful as silent loss).
5. **ORPHAN** — no confident hit.

The fuzzy stage adds the ``diff-match-patch`` dependency. Structural
spans have NO fuzzy stage (a sibling bead) — keys are identity-matched
only.
"""

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from diff_match_patch import diff_match_patch

from setforge.errors import AnchorAmbiguousError, AnchorNotFoundError
from setforge.markdown_spans import (
    MarkdownSpan,
    _find_heading_lines,
    _parse_anchor,
    _scan_end,
    bound_span,
    heading_level,
)
from setforge.spans_store import SpanState

__all__ = [
    "RelocationResult",
    "RelocationStatus",
    "relocate_span",
]

# Conservative fuzzy thresholds, biased to orphan. A LOWER Match_Threshold
# is STRICTER (0.0 = perfect match only). Match_Distance bounds how far
# from the hint location a match may drift. These are intentionally tight:
# a wrong confident relocation is as harmful as silent loss, so the fuzzy
# stage prefers to orphan-and-warn (Invariant I8 spirit).
_FUZZY_THRESHOLD: Final[float] = 0.2
_FUZZY_DISTANCE: Final[int] = 200


class RelocationStatus(StrEnum):
    """Outcome of a relocation attempt."""

    LOCATED = "located"
    ORPHAN = "orphan"


@dataclass(slots=True, frozen=True)
class RelocationResult:
    """Result of :func:`relocate_span`.

    ``status`` is :data:`RelocationStatus.LOCATED` with a non-``None``
    ``span`` when the span was confidently relocated, else
    :data:`RelocationStatus.ORPHAN` with ``span = None``. ``stage`` names
    the ladder rung that resolved it (for diagnostics / tests); ``None``
    on orphan.
    """

    status: RelocationStatus
    span: MarkdownSpan | None
    stage: str | None = None


def _fingerprint(body: str) -> str:
    """Return the sha256 hex digest of a span body (sections.hash style)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _bounded_at(text: str, level: int, start_line: int) -> MarkdownSpan:
    """Bound a span at a known heading ``start_line`` (no anchor re-search)."""
    end_line = _scan_end(text, start_line, level)
    body = "".join(text.splitlines(keepends=True)[start_line:end_line])
    return MarkdownSpan(
        start_line=start_line, end_line=end_line, level=level, body=body
    )


def _located(span: MarkdownSpan, stage: str) -> RelocationResult:
    return RelocationResult(status=RelocationStatus.LOCATED, span=span, stage=stage)


_ORPHAN: Final[RelocationResult] = RelocationResult(
    status=RelocationStatus.ORPHAN, span=None
)


def relocate_span(text: str, anchor: str, state: SpanState) -> RelocationResult:
    """Locate ``anchor``'s span in ``text`` via the relocation ladder.

    ``state`` is the stored :class:`SpanState` (fingerprint + context +
    advisory hint + level). Returns a :class:`RelocationResult`; an
    unrelocatable span is an ORPHAN (never a crash, never a pick-first
    guess). A malformed stored ``anchor`` (one that is not a well-formed
    ATX heading line) is itself an ORPHAN — :func:`_parse_anchor` raising
    here would crash install, breaking the "never a crash" contract that
    :func:`~setforge.spans_overlay.apply_spans` /
    :func:`~setforge.spans_overlay.exclude_spans_for_capture` rely on.
    See the module docstring for the per-stage policy.
    """
    try:
        level, heading_text = _parse_anchor(anchor)
    except (AnchorNotFoundError, AnchorAmbiguousError):
        return _ORPHAN
    heading_lines = _find_heading_lines(text, level, heading_text)

    # Bound every candidate heading once; reused across stages.
    candidates = [_bounded_at(text, level, line) for line in heading_lines]
    fingerprint_hits = [
        span for span in candidates if _fingerprint(span.body) == state.fingerprint
    ]

    # Stage 1: fingerprint exact-match at the position hint (fast path).
    for span in fingerprint_hits:
        if span.start_line == state.position_hint_start_line:
            return _located(span, "fingerprint-at-hint")

    # Stage 2: fingerprint unique-anywhere; multiple hits orphan.
    if len(fingerprint_hits) == 1:
        return _located(fingerprint_hits[0], "fingerprint-unique")
    if len(fingerprint_hits) > 1:
        return _ORPHAN

    # Stage 3: heading resolve (body edited). Duplicate heading orphans.
    try:
        resolved = bound_span(text, anchor)
    except AnchorAmbiguousError:
        return _ORPHAN
    except AnchorNotFoundError:
        resolved = None
    if resolved is not None:
        return _located(resolved, "heading-resolve")

    # Stage 4: diff-match-patch fuzzy, conservative + biased to orphan.
    fuzzy = _fuzzy_relocate(text, state)
    if fuzzy is not None:
        return _located(fuzzy, "fuzzy")

    # Stage 5: ORPHAN.
    return _ORPHAN


def _fuzzy_relocate(text: str, state: SpanState) -> MarkdownSpan | None:
    """Attempt a conservative fuzzy relocation of the span's heading line.

    Uses ``diff_match_patch.match_main`` to find ``state.anchor`` (the
    full heading line, e.g. ``"## Foo"``) near the stored hint's char
    offset. Returns ``None`` (orphan) on any of: a recorded
    ``heading_level`` outside 1-6; no fuzzy match below the conservative
    threshold; a match whose char offset lands past EOF; or a landed line
    that is NOT an ATX heading of the recorded level (the post-match
    structural guard makes the orphan bias structural, not merely
    threshold-dependent). On a confident, structurally valid match the
    heading line is re-bounded with the recorded level so the returned
    span is fence/level-correct.
    """
    if not (1 <= state.heading_level <= 6):
        return None
    # ``state.anchor`` is already the full heading line (e.g. "## Foo").
    pattern = state.anchor
    keep = text.splitlines(keepends=True)
    lines = text.splitlines()
    # Exact char start of the hint line is the search anchor — sum the
    # byte lengths of the preceding lines WITH their trailing newlines
    # (a plain "\n".join drops the newline after the last joined line and
    # biases the offset one char short).
    hint_offset = len("".join(keep[: state.position_hint_start_line]))
    dmp = diff_match_patch()
    dmp.Match_Threshold = _FUZZY_THRESHOLD
    dmp.Match_Distance = _FUZZY_DISTANCE
    char_idx = dmp.match_main(text, pattern, hint_offset)
    if char_idx < 0:
        return None
    start_line = text.count("\n", 0, char_idx)
    if start_line >= len(lines):
        return None
    # Post-match structural guard: the landed line MUST itself be an ATX
    # heading of the recorded level. A fuzzy char-offset match can drift
    # onto a non-heading or wrong-level line; accepting it would risk a
    # confident wrong-relocation (as harmful as silent loss). Re-validating
    # the structure makes the orphan-bias guarantee structural.
    if heading_level(lines[start_line]) != state.heading_level:
        return None
    return _bounded_at(text, state.heading_level, start_line)
