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
from dataclasses import dataclass
from enum import StrEnum

from setforge.host_local_inject import _normalise_eol

__all__ = ["DetectRegion", "RegionKind", "compute_detect_regions"]


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
