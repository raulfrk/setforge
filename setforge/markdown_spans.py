"""Markdown heading-scoped span bounding (Stage 3 of the span engine).

A span anchored on a markdown ATX heading covers the region
``[heading_line, boundary)`` where ``boundary`` is the line of the first
subsequent heading whose level is <= the anchor heading's level, else EOF
(the last section runs to end-of-file). Deeper-level children fall inside
the span for free — ``pin "## Foo"`` is inclusive of its ``###`` children.

The anchor grammar here is the FULL heading line (e.g. ``"## My Tweaks"``)
rather than the bare text the user-section anchors match: the ``#``-run
encodes the level so the boundary scan knows where the section ends, and
the trailing text is matched byte-exact (no slug / case-fold), reusing the
same fence-aware skip as :mod:`setforge.host_local_inject` so a
heading-shaped line inside a fenced code block neither matches the anchor
nor closes the span. Setext (underline) headings are unsupported (ATX
only), a documented constraint.

A *breadcrumb* anchor joins full heading lines with ``" > "`` (e.g.
``"## Final checks > ### Failure handling"``) to disambiguate a heading
whose text+level repeats under different parents. Detection is
back-compatible: a string is a breadcrumb only when EVERY ``" > "``
segment parses as an ATX heading — ``"### Use A > B form"`` stays a
simple literal anchor because ``"B form"`` is not a heading. The one
residual collision is a literal heading whose own text contains
``" > #"`` followed by heading-shaped text: the breadcrumb
interpretation WINS there (such a heading cannot be addressed by a
simple anchor), a documented constraint. Resolution matches the LEAF
segment whose ancestor chain — parent = nearest preceding heading of
STRICTLY lower level, fence-aware — ends with the breadcrumb's segments
as a suffix (the chain need not start at the root); the end boundary
scans from the leaf at the LEAF's level.
"""

import re
from dataclasses import dataclass
from typing import Final

from setforge.errors import AnchorAmbiguousError, AnchorNotFoundError
from setforge.host_local_inject import _FENCE_RE

# ATX heading with its ``#``-run captured (group 1 = the hashes, group 2 =
# the trimmed text). Widened from host_local_inject._HEADING_RE — which
# discards the run — so the span engine can read the heading LEVEL.
_HEADING_LEVEL_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")


def heading_level(line: str) -> int | None:
    """Return the ATX heading level (1-6) of ``line``, or ``None``.

    ``None`` when ``line`` is not an ATX heading. Fence awareness is the
    caller's concern — this is a pure per-line classifier.
    """
    match = _HEADING_LEVEL_RE.match(line)
    if match is None:
        return None
    return len(match.group(1))


def _parse_anchor(anchor: str) -> tuple[int, str]:
    """Split a heading-line anchor into ``(level, trimmed_text)``.

    Raises :class:`AnchorNotFoundError` when ``anchor`` is not a
    well-formed ATX heading line (the only span anchor shape this release
    supports).
    """
    match = _HEADING_LEVEL_RE.match(anchor)
    if match is None:
        raise AnchorNotFoundError(
            f"span anchor {anchor!r} is not a markdown ATX heading line "
            "(e.g. '## My Tweaks')"
        )
    return len(match.group(1)), match.group(2)


# Separator between full-heading segments of a breadcrumb anchor.
_BREADCRUMB_SEP: Final[str] = " > "


def _split_breadcrumb(anchor: str) -> list[tuple[int, str]] | None:
    """Split ``anchor`` into breadcrumb ``(level, text)`` segments, or ``None``.

    ``None`` unless the ``" > "`` split yields at least two parts AND every
    part parses as an ATX heading — the back-compat detection rule that
    keeps ``"### Use A > B form"`` a simple literal anchor. An empty
    segment (trailing separator, doubled separator) fails the per-part
    heading parse, so malformed chains fall through to the simple-anchor
    path and surface as a clean :class:`AnchorNotFoundError` there.
    """
    parts = anchor.split(_BREADCRUMB_SEP)
    if len(parts) < 2:
        return None
    segments: list[tuple[int, str]] = []
    for part in parts:
        match = _HEADING_LEVEL_RE.match(part)
        if match is None:
            return None
        segments.append((len(match.group(1)), match.group(2)))
    return segments


def _scan_headings(text: str) -> list[tuple[int, int, str]]:
    """Return every fence-aware heading as ``(line_idx, level, trimmed_text)``.

    The single scan both the breadcrumb leaf match and the ancestor walk
    run on, so a heading-shaped line inside a fenced code block is
    invisible to EVERY breadcrumb segment (same ``_FENCE_RE`` toggle as
    :func:`_find_heading_lines`).
    """
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    for idx, line in enumerate(text.splitlines()):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_LEVEL_RE.match(line)
        if match is None:
            continue
        headings.append((idx, len(match.group(1)), match.group(2)))
    return headings


def _ancestor_chain(
    headings: list[tuple[int, int, str]], pos: int
) -> list[tuple[int, str]]:
    """Return the ``(level, text)`` chain root → leaf for ``headings[pos]``.

    Walks backward from the leaf: each parent is the NEAREST preceding
    heading of STRICTLY lower level (the same boundary logic
    :func:`_scan_end` applies forward), so a same-level predecessor is a
    sibling, never a parent, and level-skips (``##`` then ``####``)
    parent onto the ``##``.
    """
    _line, level, text = headings[pos]
    chain = [(level, text)]
    current_level = level
    for idx in range(pos - 1, -1, -1):
        _l, lvl, txt = headings[idx]
        if lvl < current_level:
            chain.append((lvl, txt))
            current_level = lvl
    chain.reverse()
    return chain


def _resolve_breadcrumb(text: str, segments: list[tuple[int, str]]) -> list[int]:
    """Return every leaf start line whose ancestor chain matches ``segments``.

    A leaf matches when its ``(level, text)`` equals the LAST segment and
    its ancestor chain ends with ``segments`` as a suffix — the chain need
    not start at the document root. ALL matches are collected so the
    caller can distinguish not-found / unique / still-ambiguous
    (Invariant I8: never pick-first).
    """
    headings = _scan_headings(text)
    leaf = segments[-1]
    matches: list[int] = []
    for pos, (line_idx, level, heading_text) in enumerate(headings):
        if (level, heading_text) != leaf:
            continue
        chain = _ancestor_chain(headings, pos)
        if len(chain) >= len(segments) and chain[-len(segments) :] == segments:
            matches.append(line_idx)
    return matches


def _segment_str(level: int, text: str) -> str:
    """Render a ``(level, text)`` segment back to its heading-line form."""
    return f"{'#' * level} {text}"


def _breadcrumb_suggestions(
    text: str, matches: list[int], level: int, heading_text: str
) -> list[str]:
    """Return one disambiguating breadcrumb per duplicate occurrence.

    For each occurrence of the ambiguous simple anchor, ancestors are
    prepended (leaf-outward) until the resulting breadcrumb resolves to
    that occurrence alone; an occurrence whose FULL chain is still shared
    (identical parent paths) yields no suggestion — no breadcrumb can
    disambiguate it.
    """
    headings = _scan_headings(text)
    pos_by_line = {line: pos for pos, (line, _lvl, _txt) in enumerate(headings)}
    chains = [
        _ancestor_chain(headings, pos_by_line[line])
        for line in matches
        if line in pos_by_line
    ]
    suggestions: list[str] = []
    for chain in chains:
        for depth in range(2, len(chain) + 1):
            suffix = chain[-depth:]
            shared = sum(
                1
                for other in chains
                if len(other) >= depth and other[-depth:] == suffix
            )
            if shared == 1:
                suggestions.append(
                    _BREADCRUMB_SEP.join(_segment_str(lvl, txt) for lvl, txt in suffix)
                )
                break
    return suggestions


@dataclass(slots=True, frozen=True)
class MarkdownSpan:
    """A resolved markdown span region.

    ``start_line`` / ``end_line`` are 0-indexed half-open line offsets
    ``[start_line, end_line)`` into the file's ``splitlines(keepends=True)``
    view. ``level`` is the anchor heading's ATX level. ``body`` is the
    span's text (the lines in the half-open range joined).
    """

    start_line: int
    end_line: int
    level: int
    body: str


def _find_heading_lines(text: str, level: int, heading_text: str) -> list[int]:
    """Return every 0-indexed line whose heading matches ``(level, text)``.

    Skips lines inside fenced code blocks via the same ``_FENCE_RE`` toggle
    the host-local injector uses, so a heading-shaped line inside a fence
    never matches. Matching is byte-exact on both the level and the
    trimmed text.
    """
    matches: list[int] = []
    in_fence = False
    for idx, line in enumerate(text.splitlines()):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_LEVEL_RE.match(line)
        if match is None:
            continue
        if len(match.group(1)) == level and match.group(2) == heading_text:
            matches.append(idx)
    return matches


def _scan_end(text: str, start_line: int, level: int) -> int:
    """Return the half-open end line of the span starting at ``start_line``.

    Scans forward from the line AFTER ``start_line`` to the first heading
    whose level is <= ``level``, returning that heading's line index; else
    EOF (the total line count). The fence toggle is RE-RUN from the start
    of the file up to ``start_line`` so a span that itself opens inside (or
    after) a fence is bounded correctly — a ``#``-line inside a fenced code
    block must NOT close the span (the most likely bug-injection site).
    """
    lines = text.splitlines()
    # Re-establish fence state up to and including the heading line.
    in_fence = False
    for idx in range(start_line + 1):
        if _FENCE_RE.match(lines[idx]):
            in_fence = not in_fence
    for idx in range(start_line + 1, len(lines)):
        line = lines[idx]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        this_level = heading_level(line)
        if this_level is not None and this_level <= level:
            return idx
    return len(lines)


def bound_span(text: str, anchor: str) -> MarkdownSpan:
    """Resolve ``anchor`` against ``text`` into a :class:`MarkdownSpan`.

    Raises :class:`AnchorNotFoundError` when the heading is absent and
    :class:`AnchorAmbiguousError` when the heading text+level matches more
    than once (Invariant I8: ambiguity orphans, never picks-first — the
    caller treats the raised error as an orphan signal at relocation
    time). A breadcrumb anchor (see the module docstring) is dispatched
    BEFORE the simple-anchor parse — the heading regex would otherwise
    swallow a whole breadcrumb as one heading line — and resolves through
    the ancestor-suffix match; a simple anchor's ambiguity error
    enumerates the disambiguating breadcrumb forms. The end boundary is
    the level-aware fence-aware scan (at the LEAF level for a
    breadcrumb), so a span is inclusive of its deeper-level children.
    """
    segments = _split_breadcrumb(anchor)
    if segments is not None:
        crumbs = _resolve_breadcrumb(text, segments)
        if not crumbs:
            raise AnchorNotFoundError(
                f"no heading chain matched breadcrumb span anchor {anchor!r}"
            )
        if len(crumbs) > 1:
            lines_1 = ", ".join(str(m + 1) for m in crumbs)
            raise AnchorAmbiguousError(
                f"breadcrumb span anchor {anchor!r} matches multiple heading "
                f"chains at lines {lines_1}"
            )
        leaf_level = segments[-1][0]
        return _bounded_span(text, crumbs[0], leaf_level)
    level, heading_text = _parse_anchor(anchor)
    matches = _find_heading_lines(text, level, heading_text)
    if not matches:
        raise AnchorNotFoundError(f"no heading matched span anchor {anchor!r}")
    if len(matches) > 1:
        lines_1 = ", ".join(str(m + 1) for m in matches)
        suggestions = _breadcrumb_suggestions(text, matches, level, heading_text)
        hint = (
            "; disambiguate with a breadcrumb anchor: "
            + "; ".join(repr(s) for s in suggestions)
            if suggestions
            else "; the occurrences share identical heading chains, so no "
            "breadcrumb anchor can disambiguate them"
        )
        raise AnchorAmbiguousError(
            f"span anchor {anchor!r} matches multiple headings at lines {lines_1}{hint}"
        )
    start_line = matches[0]
    return _bounded_span(text, start_line, level)


def _bounded_span(text: str, start_line: int, level: int) -> MarkdownSpan:
    """Bound the span at a resolved ``start_line`` of a ``level`` heading."""
    end_line = _scan_end(text, start_line, level)
    keep = text.splitlines(keepends=True)
    body = "".join(keep[start_line:end_line])
    return MarkdownSpan(
        start_line=start_line, end_line=end_line, level=level, body=body
    )
