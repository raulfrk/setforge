"""Detect host-local edits in a deployed file → carve-ready regions (9hrw S2).

``setforge section detect`` diffs the live file against the EXPECTED DEPLOY
OUTPUT — what deploy would write given the current tracked source plus existing
spans / overlays / markers — and surfaces the regions the user hand-edited,
classified by whether they are new host-local content (overlay-bound) or a
divergence from an existing tracked section (pinned/forked-bound).

This module is PURE over text: the caller supplies the expected output (via
:func:`setforge.deploy.resolve_deploy`) and the live bytes; this computes the
regions. Diffing against the expected DEPLOY output — not the raw tracked source
— is what keeps deploy-side normalization (CRLF collapse, trailing-newline
pinning, marker-hash re-stamp, marker/overlay injection) from reading as user
edits, and gives the idempotency property: detect run immediately after install,
with no hand-edits, finds nothing. Regions already covered by an existing span
do not appear because they are already reflected in the expected output.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from enum import StrEnum

from setforge.anchors import (
    Anchor,
    AnchorAfterHeading,
    AnchorAtEndOfFile,
    AnchorAtStartOfFile,
)
from setforge.host_local_inject import _normalise_eol
from setforge.markdown_spans import _scan_headings

__all__ = [
    "AnchorRefusal",
    "DetectRegion",
    "RegionKind",
    "compute_detect_regions",
    "propose_anchor",
]

#: A setext underline: a run of only ``=`` or only ``-`` (a non-empty line),
#: which turns the preceding text line into a heading. Such headings are NOT
#: recognised by the ATX-only span/anchor machinery, so a region under one is
#: refused rather than mis-anchored to a distant ATX heading.
_SETEXT_RE: re.Pattern[str] = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_FENCE_RE: re.Pattern[str] = re.compile(r"^\s{0,3}(```|~~~)")


class RegionKind(StrEnum):
    """How a detected region relates to the tracked source.

    ``new-content`` — lines present in live but absent from the expected deploy
    output (a pure insertion): host-local content with no tracked anchor, carved
    as an OVERLAY span. ``divergence`` — lines that replace or delete existing
    expected content (you changed/trimmed a tracked section), carved as a
    PINNED/FORKED span.
    """

    NEW_CONTENT = "new-content"
    DIVERGENCE = "divergence"


@dataclass(slots=True, frozen=True)
class DetectRegion:
    """One contiguous run where live diverges from the expected deploy output.

    Line indices are 0-based, half-open, into the EOL-normalized line lists
    (``splitlines(keepends=True)``). ``live_start``..``live_end`` indexes the
    live lines; ``expected_start``..``expected_end`` indexes the expected lines
    (an empty range for a pure insertion). ``live_text`` / ``expected_text`` are
    the joined bytes of those ranges, used as the carve body / context.
    """

    kind: RegionKind
    live_start: int
    live_end: int
    expected_start: int
    expected_end: int
    live_text: str
    expected_text: str


def compute_detect_regions(live: str, expected: str) -> list[DetectRegion]:
    """Return the regions where ``live`` diverges from the ``expected`` output.

    Both inputs are EOL-normalized to LF before diffing, so a CRLF live file on
    one host does not read as drift. Returns an empty list when live equals
    expected after normalization (idempotency). Each non-``equal`` difflib
    opcode becomes one :class:`DetectRegion`: an ``insert`` is ``NEW_CONTENT``,
    a ``replace`` or ``delete`` is ``DIVERGENCE``. ``autojunk`` is disabled so
    large or repetitive files are not silently coarsened.
    """
    live_lines = _normalise_eol(live).splitlines(keepends=True)
    expected_lines = _normalise_eol(expected).splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, expected_lines, live_lines, autojunk=False)
    regions: list[DetectRegion] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        kind = RegionKind.NEW_CONTENT if tag == "insert" else RegionKind.DIVERGENCE
        regions.append(
            DetectRegion(
                kind=kind,
                live_start=j1,
                live_end=j2,
                expected_start=i1,
                expected_end=i2,
                live_text="".join(live_lines[j1:j2]),
                expected_text="".join(expected_lines[i1:i2]),
            )
        )
    return regions


@dataclass(slots=True, frozen=True)
class AnchorRefusal:
    """Why a detected region could not be given a safe markerless anchor.

    Returned by :func:`propose_anchor` instead of an :data:`Anchor` when the
    region cannot be anchored without risking a wrong-location splice or an
    install-time orphan: an ambiguous heading with no unique disambiguation, a
    setext or closing-hash heading the ATX machinery cannot match, a heading
    present in live but absent from the tracked source, or a divergence with no
    enclosing heading at all. The carve wizard surfaces the ``reason`` and skips
    the region (the user can add/clean up a heading and re-run).
    """

    reason: str


def _heading_text_count(text: str, level: int, heading_text: str) -> int:
    """Count fence-aware ATX headings in ``text`` matching ``(level, text)``."""
    return sum(
        1
        for _idx, lvl, txt in _scan_headings(text)
        if lvl == level and txt == heading_text
    )


def _setext_heading_between(lines: list[str], start: int, end: int) -> bool:
    """True if a setext underline appears in ``lines[start:end]`` outside fences.

    Catches the case where the region's true enclosing heading is a setext
    heading (closer than any ATX heading), which the ATX-only anchor machinery
    would silently miss.
    """
    in_fence = False
    for line in lines[start:end]:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and _SETEXT_RE.match(line) and line.strip():
            return True
    return False


def propose_anchor(
    region: DetectRegion, live: str, expected: str
) -> Anchor | AnchorRefusal:
    """Propose a safe markerless anchor for ``region``, or refuse.

    The anchor is the **immediately-enclosing ATX heading** (nearest preceding
    heading of any level), as an :class:`AnchorAfterHeading`. New content with
    no enclosing heading anchors at the file boundary
    (:class:`AnchorAtStartOfFile` / :class:`AnchorAtEndOfFile`). The proposal is
    refused (returns :class:`AnchorRefusal`) when the enclosing heading is
    ambiguous (appears more than once in live or in the tracked source), is a
    setext or closing-hash heading, or is absent from the tracked source (it
    would orphan on install); and when a divergence has no enclosing heading.

    Heading-text matching reuses the fence-aware
    :func:`setforge.markdown_spans._scan_headings`, so a heading-shaped line
    inside a fenced code block is never chosen.
    """
    live_n = _normalise_eol(live)
    expected_n = _normalise_eol(expected)
    live_lines = live_n.splitlines()
    headings = _scan_headings(live_n)  # [(line_idx, level, text)] in file order

    enclosing: tuple[int, int, str] | None = None
    for line_idx, level, htext in headings:
        if line_idx <= region.live_start:
            enclosing = (line_idx, level, htext)
        else:
            break

    # A setext heading between the enclosing ATX heading (or file start) and the
    # region is the region's true heading but is invisible to the ATX machinery
    # — refuse rather than mis-anchor to a distant ATX heading or fall through.
    search_start = (enclosing[0] + 1) if enclosing is not None else 0
    if _setext_heading_between(live_lines, search_start, region.live_start + 1):
        return AnchorRefusal("region sits under a setext heading (unsupported)")

    if enclosing is None:
        if region.kind is RegionKind.NEW_CONTENT:
            if region.live_start == 0:
                return AnchorAtStartOfFile()
            return AnchorAtEndOfFile()
        return AnchorRefusal(
            "region modifies content with no enclosing heading to anchor to"
        )

    _heading_line, level, heading_text = enclosing
    return _validated_heading_anchor(level, heading_text, live_n, expected_n)


def _validated_heading_anchor(
    level: int, heading_text: str, live_n: str, expected_n: str
) -> Anchor | AnchorRefusal:
    """Return an :class:`AnchorAfterHeading` for ``heading_text`` or refuse.

    Refuses a closing-hash heading, a heading ambiguous in live, a heading
    absent from the tracked source (would orphan), and a heading ambiguous in
    the tracked source. ``live_n`` / ``expected_n`` are EOL-normalized.
    """
    if heading_text.rstrip().endswith("#"):
        return AnchorRefusal(
            f"closing-hash heading {heading_text!r} is not safely anchorable"
        )
    live_count = _heading_text_count(live_n, level, heading_text)
    expected_count = _heading_text_count(expected_n, level, heading_text)
    if live_count != 1:
        return AnchorRefusal(
            f"heading {heading_text!r} is ambiguous (appears {live_count} times "
            "in live); add a unique heading and re-run"
        )
    if expected_count == 0:
        return AnchorRefusal(
            f"heading {heading_text!r} is not in the tracked source; the anchor "
            "would orphan on install"
        )
    if expected_count != 1:
        return AnchorRefusal(
            f"heading {heading_text!r} is ambiguous in the tracked source "
            f"(appears {expected_count} times)"
        )
    return AnchorAfterHeading(value=heading_text)
