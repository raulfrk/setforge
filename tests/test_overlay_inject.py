"""Unit tests for the overlay body canonicalisation and inject primitives.

These exercise :mod:`setforge.body_canon` in isolation — the pure text
functions the host-local seed and the old-schema migrations build on.
"""

from __future__ import annotations

import pytest

from setforge.body_canon import (
    canonical_body,
    inject_body_at_anchor,
)
from setforge.errors import AnchorNotFoundError
from setforge.source import AnchorAfterHeading, AnchorAtEndOfFile


def test_canonical_body_normalises_eol_and_single_trailing_newline() -> None:
    assert canonical_body("a\r\nb") == "a\nb\n"
    assert canonical_body("a\nb\n\n\n") == "a\nb\n"
    assert canonical_body("a\nb") == "a\nb\n"
    assert canonical_body("a\nb\n") == "a\nb\n"


def test_inject_after_heading_places_body_below_heading() -> None:
    text = "# Title\n\n## Notes\n\nshared body\n"
    body = canonical_body("HOST LOCAL ONLY")
    out = inject_body_at_anchor(text, AnchorAfterHeading(value="Notes"), body)
    assert body in out
    # Body lands immediately after the heading line.
    lines = out.splitlines()
    notes_idx = lines.index("## Notes")
    assert lines[notes_idx + 1] == "HOST LOCAL ONLY"


def test_inject_at_end_of_file() -> None:
    text = "# Title\n"
    body = canonical_body("TAIL")
    out = inject_body_at_anchor(text, AnchorAtEndOfFile(), body)
    assert out.endswith("TAIL\n")


def test_inject_missing_anchor_raises() -> None:
    with pytest.raises(AnchorNotFoundError):
        inject_body_at_anchor(
            "# Title\n", AnchorAfterHeading(value="Nope"), canonical_body("x")
        )


def test_crlf_body_injects_byte_exact() -> None:
    # A CRLF-authored body canonicalises to LF and injects byte-exact.
    text = "# Title\n\n## Notes\n\nshared\n"
    body = canonical_body("HOST LOCAL\r\nBODY")
    injected = inject_body_at_anchor(text, AnchorAfterHeading(value="Notes"), body)
    assert injected == "# Title\n\n## Notes\nHOST LOCAL\nBODY\n\nshared\n"
