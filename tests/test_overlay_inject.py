"""Unit tests for the leak-safe overlay inject / excise primitives.

These exercise :mod:`setforge.overlay_inject` in isolation — the pure
text functions whose identity is the exact recorded body BYTES (the needle
set), never a re-derived anchor / structure / offset. The deploy + capture
seams build on these; the leak gate ultimately rests here.
"""

from __future__ import annotations

import pytest

from setforge.errors import AnchorNotFoundError
from setforge.overlay_inject import (
    OverlayAmbiguousError,
    canonical_body,
    excise_unique_needle,
    inject_body_at_anchor,
)
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


def test_excise_unique_needle_round_trips_with_inject() -> None:
    text = "# Title\n\n## Notes\n\nshared\n"
    body = canonical_body("HOST LOCAL")
    injected = inject_body_at_anchor(text, AnchorAfterHeading(value="Notes"), body)
    excised, found = excise_unique_needle(injected, [body])
    assert found == body
    assert "HOST LOCAL" not in excised
    # Excision is a length-exact splice — no seam-collapse beyond the needle.
    assert excised == text


def test_excise_zero_occurrence_returns_none() -> None:
    excised, found = excise_unique_needle("# Title\n", [canonical_body("absent")])
    assert found is None
    assert excised == "# Title\n"


def test_excise_ambiguous_needle_raises() -> None:
    body = canonical_body("DUP")
    text = f"a\n{body}b\n{body}c\n"
    with pytest.raises(OverlayAmbiguousError):
        excise_unique_needle(text, [body])


def test_excise_prefers_first_needle_in_sequence_order() -> None:
    # The needle sequence is tried in order; the first unique hit wins.
    canonical = canonical_body("CANON")
    prior = canonical_body("PRIOR")
    text = f"head\n{prior}tail\n"
    excised, found = excise_unique_needle(text, [canonical, prior])
    assert found == prior
    assert "PRIOR" not in excised
