"""Unit tests for the A5 per-hunk staging model (:mod:`setforge.reconcile.hunks`).

Covers hunk extraction (base↔live 2-way diff), identity (EOL-normalised content
hash + base-context anchor), classification carry-over, reconstruction of the
shared-promotion, and the INV-8 stage-fidelity assertion.
"""

from __future__ import annotations

import pytest

from setforge.errors import InvariantViolation
from setforge.reconcile.hunks import (
    Hunk,
    assert_stage_fidelity,
    classify,
    extract_hunks,
    identity,
    serialize,
)
from setforge.reconcile.types import HunkClass

# A base/live pair with two independent changes:
#   - an inserted "## Shell" block (generally useful → SHARE candidate)
#   - a changed host path under "## Host paths" (host-specific → LOCAL candidate)
BASE = b"## Tool prefs\nUse rg not grep.\n\n## Host paths\nworkdir: /home/generic\n"
LIVE = (
    b"## Tool prefs\nUse rg not grep.\n\n"
    b"## Shell\nPrefer zsh.\n\n"
    b"## Host paths\nworkdir: /home/raul\n"
)


def _by_label(hunks: list[Hunk]) -> dict[str, Hunk]:
    return {h.label: h for h in hunks}


# --------------------------------------------------------------------------- #
# extract_hunks
# --------------------------------------------------------------------------- #


def test_no_change_yields_no_hunks() -> None:
    assert extract_hunks(BASE, BASE) == []


def test_extract_finds_each_changed_region() -> None:
    hunks = extract_hunks(BASE, LIVE)
    # the inserted Shell block and the changed workdir line are distinct hunks.
    labels = {h.label for h in hunks}
    assert "## Shell" in labels
    assert "## Host paths" in labels  # workdir change → nearest preceding heading
    assert all(h.cls is HunkClass.PENDING for h in hunks)  # unclassified until staged


def test_pure_insertion_has_empty_base_span() -> None:
    hunks = _by_label(extract_hunks(BASE, LIVE))
    shell = hunks["## Shell"]
    assert shell.base_span[0] == shell.base_span[1]  # nothing replaced in base
    assert shell.live_span[1] > shell.live_span[0]  # live gained lines


def test_label_falls_back_to_changed_line_without_heading() -> None:
    base = b"alpha\nbeta\ngamma\n"
    live = b"alpha\nBETA-EDITED\ngamma\n"
    (hunk,) = extract_hunks(base, live)
    assert hunk.label == "BETA-EDITED"


# --------------------------------------------------------------------------- #
# identity — EOL / context stability
# --------------------------------------------------------------------------- #


def test_eol_only_change_keeps_identity_stable() -> None:
    # The changed line's terminator differs (LF vs CRLF) while the surrounding
    # context stays aligned — the identity must not re-mint on the terminator.
    base = b"alpha\nbeta\ngamma\n"
    live_lf = b"alpha\nBETA-NEW\ngamma\n"
    live_crlf = b"alpha\nBETA-NEW\r\ngamma\n"
    (a,) = extract_hunks(base, live_lf)
    (b,) = extract_hunks(base, live_crlf)
    assert identity(a) == identity(b)  # CRLF vs LF must not re-mint a hunk's identity


def test_trailing_whitespace_change_keeps_identity_stable() -> None:
    base = b"alpha\nbeta\n"
    live_a = b"alpha\nBETA\n"
    live_b = b"alpha\nBETA   \n"  # same content, trailing spaces
    (a,) = extract_hunks(base, live_a)
    (b,) = extract_hunks(base, live_b)
    assert identity(a) == identity(b)


def test_edit_above_does_not_remint_lower_hunk() -> None:
    # Two lives that change the SAME lower region identically but differ above it.
    base = b"top\n\n## Section\nvalue: 1\n"
    live1 = b"top\n\n## Section\nvalue: 2\n"
    live2 = b"TOP-CHANGED\n\n## Section\nvalue: 2\n"
    lower1 = _by_label(extract_hunks(base, live1))["## Section"]
    lower2 = _by_label(extract_hunks(base, live2))["## Section"]
    assert identity(lower1) == identity(lower2)


# --------------------------------------------------------------------------- #
# classify — carry-over by identity
# --------------------------------------------------------------------------- #


def test_classify_carries_stored_class_for_stable_hunk() -> None:
    fresh = extract_hunks(BASE, LIVE)
    shell = _by_label(fresh)["## Shell"]
    stored: list[dict[str, object]] = [
        {
            "cls": HunkClass.SHARED.value,
            "label": shell.label,
            "live_hash": shell.live_hash,
            "anchor": shell.anchor,
        }
    ]
    classified = _by_label(classify(fresh, stored))
    assert classified["## Shell"].cls is HunkClass.SHARED
    assert classified["## Shell"].changed is False
    # the unmentioned workdir hunk stays PENDING
    assert classified["## Host paths"].cls is HunkClass.PENDING


def test_classify_unmatched_is_pending() -> None:
    fresh = extract_hunks(BASE, LIVE)
    classified = classify(fresh, [])
    assert all(h.cls is HunkClass.PENDING for h in classified)


def test_classify_anchor_stable_but_changed_keeps_class_flagged() -> None:
    # stage the workdir hunk SHARED, then the host edits the value again.
    fresh1 = _by_label(extract_hunks(BASE, LIVE))["## Host paths"]
    stored: list[dict[str, object]] = [
        {
            "cls": HunkClass.SHARED.value,
            "label": fresh1.label,
            "live_hash": fresh1.live_hash,
            "anchor": fresh1.anchor,
        }
    ]
    live2 = LIVE.replace(b"workdir: /home/raul", b"workdir: /home/elsewhere")
    fresh2 = classify(extract_hunks(BASE, live2), stored)
    wd = _by_label(fresh2)["## Host paths"]
    assert wd.cls is HunkClass.SHARED  # class survives the value edit
    assert wd.changed is True  # but it's flagged for re-confirm, not silently reset


# --------------------------------------------------------------------------- #
# reconstruct (via assert_stage_fidelity round-trips) + serialize
# --------------------------------------------------------------------------- #


def _stage(base: bytes, live: bytes, classes: dict[str, HunkClass]) -> list[Hunk]:
    """Extract and assign each hunk the class named for its label."""
    out = []
    for h in extract_hunks(base, live):
        out.append(
            Hunk(
                cls=classes.get(h.label, HunkClass.PENDING),
                label=h.label,
                live_hash=h.live_hash,
                anchor=h.anchor,
                base_span=h.base_span,
                live_span=h.live_span,
            )
        )
    return out


def test_reconstruct_promotes_only_shared() -> None:
    from setforge.reconcile.hunks import reconstruct

    hunks = _stage(
        BASE, LIVE, {"## Shell": HunkClass.SHARED, "## Host paths": HunkClass.LOCAL}
    )
    tracked = reconstruct(BASE, LIVE, hunks)
    assert b"## Shell" in tracked  # SHARED promoted
    assert b"Prefer zsh." in tracked
    assert b"workdir: /home/generic" in tracked  # LOCAL kept base, NOT live
    assert b"workdir: /home/raul" not in tracked


def test_reconstruct_all_pending_equals_base() -> None:
    from setforge.reconcile.hunks import reconstruct

    hunks = _stage(BASE, LIVE, {})  # all PENDING
    assert reconstruct(BASE, LIVE, hunks) == BASE


def test_reconstruct_demote_reverts_region_to_base() -> None:
    from setforge.reconcile.hunks import reconstruct

    shared = _stage(BASE, LIVE, {"## Host paths": HunkClass.SHARED})
    assert b"workdir: /home/raul" in reconstruct(BASE, LIVE, shared)
    demoted = _stage(BASE, LIVE, {"## Host paths": HunkClass.LOCAL})
    out = reconstruct(BASE, LIVE, demoted)
    assert b"workdir: /home/raul" not in out  # demote un-captures
    assert b"workdir: /home/generic" in out


# --------------------------------------------------------------------------- #
# assert_stage_fidelity (INV-8)
# --------------------------------------------------------------------------- #


def test_fidelity_passes_for_correct_tracked() -> None:
    from setforge.reconcile.hunks import reconstruct

    hunks = _stage(BASE, LIVE, {"## Shell": HunkClass.SHARED})
    tracked = reconstruct(BASE, LIVE, hunks)
    assert_stage_fidelity(BASE, LIVE, tracked, hunks)  # no raise


def test_fidelity_raises_when_local_bytes_leak_into_tracked() -> None:
    hunks = _stage(BASE, LIVE, {"## Host paths": HunkClass.LOCAL})
    leaked = LIVE  # tracked == full live → a LOCAL hunk leaked
    with pytest.raises(InvariantViolation, match="INV-8"):
        assert_stage_fidelity(BASE, LIVE, leaked, hunks)


def test_serialize_drops_transient_spans() -> None:
    hunks = _stage(BASE, LIVE, {"## Shell": HunkClass.SHARED})
    rows = serialize(hunks)
    assert rows == [
        {
            "cls": h.cls.value,
            "label": h.label,
            "live_hash": h.live_hash,
            "anchor": h.anchor,
        }
        for h in hunks
    ]
    assert all("base_span" not in row and "live_span" not in row for row in rows)
