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
    time). The end boundary is the level-aware fence-aware scan, so a span
    is inclusive of its deeper-level children.
    """
    level, heading_text = _parse_anchor(anchor)
    matches = _find_heading_lines(text, level, heading_text)
    if not matches:
        raise AnchorNotFoundError(f"no heading matched span anchor {anchor!r}")
    if len(matches) > 1:
        lines_1 = ", ".join(str(m + 1) for m in matches)
        raise AnchorAmbiguousError(
            f"span anchor {anchor!r} matches multiple headings at lines "
            f"{lines_1}; duplicate-heading spans are unsupported this release"
        )
    start_line = matches[0]
    end_line = _scan_end(text, start_line, level)
    keep = text.splitlines(keepends=True)
    body = "".join(keep[start_line:end_line])
    return MarkdownSpan(
        start_line=start_line, end_line=end_line, level=level, body=body
    )
