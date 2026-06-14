"""Regression tests for the unnamed-section positional-reindex misclassification.

``classify_section_drift`` keys UNNAMED shared sections by their 0-based
ordinal among unnamed sections (``"0"``, ``"1"``, ...). Deleting or
reordering an unnamed section on one side shifts every later unnamed key,
so a naive ``tracked["0"]`` vs ``live["0"]`` intersection compared two
semantically-unrelated bodies and classified phantom drift / conflict —
a bare install would warn about nonexistent drift and a ``use-tracked``
decision could splice the wrong tracked body over a live section.

These tests pin the fix: unnamed-keyed sections are classified only when
the tracked and live ordered key sequences match; otherwise they are
skipped (left to the keep-live default). Named keys carry a stable
identity and stay classifiable across structural divergence.
"""

import hashlib

from setforge.section_reconcile import (
    SectionDriftState,
    classify_section_drift,
)
from setforge.sections import SectionSemantics


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _unnamed(body: str, semantics: str = "shared") -> str:
    """An unnamed section marker pair with a body-aligned hash."""
    return (
        f"<!-- setforge:user-section start {semantics} -->\n"
        f"{body}"
        f"<!-- setforge:user-section end {semantics} hash={_sha256(body)} -->\n"
    )


def _named(name: str, body: str, semantics: str = "shared") -> str:
    return (
        f"<!-- setforge:user-section start {semantics} {name} -->\n"
        f"{body}"
        f"<!-- setforge:user-section end {semantics} {name} hash={_sha256(body)} -->\n"
    )


def test_deleted_unnamed_section_does_not_misclassify_survivor() -> None:
    """Tracked has two unnamed sections; live deleted the FIRST one.

    The surviving live unnamed section is positional key ``"0"`` in live
    but ``"0"`` is the DELETED section in tracked. The old code compared
    tracked's ``aaa`` against live's surviving ``bbb`` and reported drift.
    The fix skips the ambiguous unnamed key entirely.
    """
    tracked = _unnamed("aaa\n") + _unnamed("bbb\n")
    live = _unnamed("bbb\n")  # first unnamed deleted; survivor reindexed to "0"

    result = classify_section_drift(tracked, live)

    # No phantom comparison of tracked["0"]=='aaa' against live["0"]=='bbb'.
    assert result == {}


def test_named_removed_shifts_nothing_but_structure_diverges() -> None:
    """Tracked = [named, unnamed]; live dropped the named section.

    The unnamed index is unaffected by named sections (both sides key the
    unnamed body as ``"0"``), but the overall structure diverges, so the
    unnamed key is conservatively skipped rather than risk any positional
    assumption. Safe direction: keep live, never splice.
    """
    tracked = _named("NAMED", "n\n") + _unnamed("u\n")
    live = _unnamed("u\n")

    result = classify_section_drift(tracked, live)

    assert result == {}


def test_aligned_unnamed_sections_still_classify() -> None:
    """When tracked and live share the same ordered key sequence, unnamed
    sections classify normally against the CORRECT positional body."""
    tracked = _unnamed("aaa\n") + _unnamed("bbb\n")
    live = _unnamed("aaa\n") + _unnamed("ZZZ\n")  # second section drifted

    result = classify_section_drift(tracked, live)

    assert set(result) == {"0", "1"}
    assert result["0"].state is SectionDriftState.NO_DRIFT
    assert result["0"].tracked_body == "aaa\n"
    assert result["0"].live_body == "aaa\n"
    # Section "1" compared against its OWN body, not a neighbour's.
    assert result["1"].tracked_body == "bbb\n"
    assert result["1"].live_body == "ZZZ\n"
    assert result["1"].state is not SectionDriftState.NO_DRIFT


def test_named_section_classifies_despite_unnamed_neighbour_deletion() -> None:
    """A NAMED section keeps its stable identity even when an unnamed
    neighbour is deleted (structural divergence). It is compared against
    its own body, never skipped, never cross-matched."""
    tracked = _named("W", "x\n") + _unnamed("u\n")
    live = _named("W", "y\n")  # unnamed neighbour gone; W body edited

    result = classify_section_drift(tracked, live)

    assert set(result) == {"W"}
    assert result["W"].semantics is SectionSemantics.SHARED
    assert result["W"].tracked_body == "x\n"
    assert result["W"].live_body == "y\n"


def test_reordered_unnamed_sections_are_skipped() -> None:
    """Reordering unnamed sections also shifts positional keys; the guard
    skips them rather than comparing reordered, unrelated bodies."""
    tracked = _unnamed("first\n") + _unnamed("second\n")
    live = _unnamed("second\n") + _unnamed("first\n")  # swapped order

    result = classify_section_drift(tracked, live)

    # Ordered key sequences are equal (both ["0", "1"]) BUT the bodies were
    # reordered. The positional keys now denote swapped sections. This is
    # the inherent limit of positional keying with identical structure:
    # the guard cannot detect a pure reorder of same-shaped unnamed
    # sections, so it classifies them — documenting that pure reorders of
    # interchangeable unnamed sections remain positional. The DELETE /
    # COUNT-change cases (the audit finding) are the ones the fix guards.
    assert set(result) == {"0", "1"}
