"""Tests for the install-side three-way section reconciler."""

import hashlib

import pytest

from my_setup.errors import MarkerError
from my_setup.section_reconcile import (
    SectionDrift,
    SectionDriftState,
    _classify_one,
    classify_section_drift,
    has_shared_drift,
    maintain_marker_hashes,
)
from my_setup.sections import (
    SectionSemantics,
    extract_marker_hashes,
    hash_sections,
    set_marker_hashes,
)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# _classify_one — unit table walk
# ---------------------------------------------------------------------------


def test_classify_one_no_drift_when_bodies_match() -> None:
    assert (
        _classify_one(a_t="x", a_l="x", e_t=None, e_l=None)
        is SectionDriftState.NO_DRIFT
    )


def test_classify_one_legacy_when_tracked_embedded_missing() -> None:
    assert (
        _classify_one(a_t="x", a_l="y", e_t=None, e_l="y") is SectionDriftState.LEGACY
    )


def test_classify_one_legacy_when_live_embedded_missing() -> None:
    assert (
        _classify_one(a_t="x", a_l="y", e_t="x", e_l=None) is SectionDriftState.LEGACY
    )


def test_classify_one_pending_tracked_when_live_pristine_tracked_moved() -> None:
    # A_L == E_L (live untouched), A_T != E_T (tracked moved)
    assert (
        _classify_one(a_t="new-tracked", a_l="live", e_t="old-tracked", e_l="live")
        is SectionDriftState.PENDING_TRACKED
    )


def test_classify_one_live_edited_when_live_moved_tracked_pristine() -> None:
    assert (
        _classify_one(a_t="tracked", a_l="new-live", e_t="tracked", e_l="old-live")
        is SectionDriftState.LIVE_EDITED
    )


def test_classify_one_conflict_when_both_moved() -> None:
    assert (
        _classify_one(
            a_t="new-tracked", a_l="new-live", e_t="old-tracked", e_l="old-live"
        )
        is SectionDriftState.CONFLICT
    )


def test_classify_one_inconsistent_when_both_pristine_but_bodies_differ() -> None:
    # A_L == E_L AND A_T == E_T but A_T != A_L — shouldn't happen
    assert (
        _classify_one(a_t="tracked", a_l="live", e_t="tracked", e_l="live")
        is SectionDriftState.INCONSISTENT
    )


# ---------------------------------------------------------------------------
# classify_section_drift — integration with sections primitives
# ---------------------------------------------------------------------------


def _make_text(name: str, semantics: str, body: str, embed_hash: str | None) -> str:
    """Build a tiny section text. ``embed_hash`` None → hashless end marker."""
    hash_segment = f" hash={embed_hash}" if embed_hash is not None else ""
    return (
        f"<!-- my-setup:user-section start {semantics} {name} -->\n"
        f"{body}"
        f"<!-- my-setup:user-section end {semantics} {name}{hash_segment} -->\n"
    )


def test_classify_section_drift_no_drift_when_bodies_match() -> None:
    body = "shared body\n"
    digest = _sha256(body)
    tracked = _make_text("workflow", "shared", body, digest)
    live = _make_text("workflow", "shared", body, digest)
    result = classify_section_drift(tracked, live)
    assert result["workflow"].state is SectionDriftState.NO_DRIFT
    assert result["workflow"].semantics is SectionSemantics.SHARED


def test_classify_section_drift_pending_tracked() -> None:
    """Live pristine (body matches its embedded hash); tracked has new updates."""
    live_body = "rule A\nrule B\n"
    tracked_body = "rule A\nrule B\nrule C (new)\n"
    live_text = _make_text("workflow", "shared", live_body, _sha256(live_body))
    # tracked's embedded hash is the OLD hash (last known baseline = live's hash)
    tracked_text = _make_text("workflow", "shared", tracked_body, _sha256(live_body))
    result = classify_section_drift(tracked_text, live_text)
    assert result["workflow"].state is SectionDriftState.PENDING_TRACKED


def test_classify_section_drift_live_edited() -> None:
    """Live moved off baseline; tracked unchanged from its embedded hash."""
    tracked_body = "rule A\n"
    live_body = "rule A\nlive addition\n"
    tracked_text = _make_text("workflow", "shared", tracked_body, _sha256(tracked_body))
    # live's embedded hash is OLD live (= the install-time baseline = tracked body)
    live_text = _make_text("workflow", "shared", live_body, _sha256(tracked_body))
    result = classify_section_drift(tracked_text, live_text)
    assert result["workflow"].state is SectionDriftState.LIVE_EDITED


def test_classify_section_drift_conflict() -> None:
    """Both sides moved off their baselines."""
    baseline = "rule A\n"
    live_body = "rule A\nlive change\n"
    tracked_body = "rule A\ntracked change\n"
    tracked_text = _make_text("workflow", "shared", tracked_body, _sha256(baseline))
    live_text = _make_text("workflow", "shared", live_body, _sha256(baseline))
    result = classify_section_drift(tracked_text, live_text)
    assert result["workflow"].state is SectionDriftState.CONFLICT


def test_classify_section_drift_legacy_when_tracked_hashless() -> None:
    body = "rule A\n"
    tracked_text = _make_text("workflow", "shared", body, None)
    live_text = _make_text("workflow", "shared", body + "live edit\n", _sha256(body))
    result = classify_section_drift(tracked_text, live_text)
    assert result["workflow"].state is SectionDriftState.LEGACY


def test_classify_section_drift_legacy_when_live_hashless() -> None:
    body = "rule A\n"
    tracked_text = _make_text(
        "workflow", "shared", body + "tracked edit\n", _sha256(body)
    )
    live_text = _make_text("workflow", "shared", body, None)
    result = classify_section_drift(tracked_text, live_text)
    assert result["workflow"].state is SectionDriftState.LEGACY


def test_classify_section_drift_iteration_order_matches_extract_sections() -> None:
    """Deterministic walk order: insertion order of extract_sections."""
    tracked = (
        _make_text("first", "shared", "f\n", _sha256("f\n"))
        + _make_text("second", "shared", "s\n", _sha256("s\n"))
        + _make_text("third", "host-local", "t\n", _sha256("t\n"))
    )
    live = tracked  # identical
    result = classify_section_drift(tracked, live)
    assert list(result.keys()) == ["first", "second", "third"]


def test_classify_section_drift_includes_host_local() -> None:
    """host-local sections appear in the result with their semantics tag."""
    body = "host body\n"
    digest = _sha256(body)
    tracked = _make_text("notes", "host-local", body, digest)
    live = _make_text("notes", "host-local", body + "edit\n", digest)
    result = classify_section_drift(tracked, live)
    assert result["notes"].semantics is SectionSemantics.HOST_LOCAL
    assert result["notes"].state is SectionDriftState.LIVE_EDITED


def test_classify_section_drift_skips_section_not_in_live() -> None:
    """A section only in tracked is silently skipped (placeholder path)."""
    tracked = _make_text("only-tracked", "shared", "body\n", _sha256("body\n"))
    live = "<!-- nothing here -->\n"
    result = classify_section_drift(tracked, live)
    assert result == {}


def test_classify_section_drift_propagates_marker_error() -> None:
    tracked = "<!-- my-setup:user-section start workflow -->\nbody\n"
    live = ""
    with pytest.raises(MarkerError):
        classify_section_drift(tracked, live)


# ---------------------------------------------------------------------------
# maintain_marker_hashes — invariant
# ---------------------------------------------------------------------------


def test_maintain_marker_hashes_writes_body_hashes_into_markers() -> None:
    body = "rule A\nrule B\n"
    text = _make_text("workflow", "shared", body, None)
    result = maintain_marker_hashes(text)
    assert extract_marker_hashes(result) == {"workflow": _sha256(body)}


def test_maintain_marker_hashes_replaces_stale_hash() -> None:
    body = "new body\n"
    text = _make_text("workflow", "shared", body, "deadbeef" * 8)
    result = maintain_marker_hashes(text)
    assert extract_marker_hashes(result) == {"workflow": _sha256(body)}


def test_maintain_marker_hashes_idempotent() -> None:
    body = "rule A\n"
    text = _make_text("workflow", "shared", body, None)
    once = maintain_marker_hashes(text)
    twice = maintain_marker_hashes(once)
    assert once == twice


def test_maintain_marker_hashes_invariant_extracted_equals_computed() -> None:
    """Post-install invariant: extract_marker_hashes == hash_sections."""
    text = _make_text("workflow", "shared", "body\n", None) + _make_text(
        "notes", "host-local", "host body\n", "1" * 64
    )
    result = maintain_marker_hashes(text)
    embedded = extract_marker_hashes(result)
    computed = hash_sections(result)
    assert {k: v for k, v in embedded.items() if v is not None} == computed


# ---------------------------------------------------------------------------
# has_shared_drift convenience
# ---------------------------------------------------------------------------


def test_has_shared_drift_false_when_only_host_local_drift() -> None:
    drifts: dict[str, SectionDrift] = {
        "notes": SectionDrift(
            name="notes",
            semantics=SectionSemantics.HOST_LOCAL,
            state=SectionDriftState.LIVE_EDITED,
            tracked_body="t\n",
            live_body="l\n",
        )
    }
    assert has_shared_drift(drifts) is False


def test_has_shared_drift_true_when_shared_pending_tracked() -> None:
    drifts: dict[str, SectionDrift] = {
        "workflow": SectionDrift(
            name="workflow",
            semantics=SectionSemantics.SHARED,
            state=SectionDriftState.PENDING_TRACKED,
            tracked_body="t\n",
            live_body="l\n",
        )
    }
    assert has_shared_drift(drifts) is True


def test_has_shared_drift_false_when_shared_no_drift() -> None:
    drifts: dict[str, SectionDrift] = {
        "workflow": SectionDrift(
            name="workflow",
            semantics=SectionSemantics.SHARED,
            state=SectionDriftState.NO_DRIFT,
            tracked_body="same\n",
            live_body="same\n",
        )
    }
    assert has_shared_drift(drifts) is False


def test_has_shared_drift_true_on_conflict() -> None:
    drifts: dict[str, SectionDrift] = {
        "workflow": SectionDrift(
            name="workflow",
            semantics=SectionSemantics.SHARED,
            state=SectionDriftState.CONFLICT,
            tracked_body="t\n",
            live_body="l\n",
        )
    }
    assert has_shared_drift(drifts) is True


def test_has_shared_drift_true_on_legacy() -> None:
    drifts: dict[str, SectionDrift] = {
        "workflow": SectionDrift(
            name="workflow",
            semantics=SectionSemantics.SHARED,
            state=SectionDriftState.LEGACY,
            tracked_body="t\n",
            live_body="l\n",
        )
    }
    assert has_shared_drift(drifts) is True


# ---------------------------------------------------------------------------
# round-trip with set_marker_hashes — sanity guard
# ---------------------------------------------------------------------------


def test_round_trip_set_marker_hashes_then_classify_no_drift() -> None:
    """After ``set_marker_hashes(t, hash_sections(t))`` on both sides with
    matching bodies, the classifier reports no drift."""
    body = "rule A\nrule B\n"
    base = _make_text("workflow", "shared", body, None)
    tracked = set_marker_hashes(base, hash_sections(base))
    live = set_marker_hashes(base, hash_sections(base))
    result = classify_section_drift(tracked, live)
    assert result["workflow"].state is SectionDriftState.NO_DRIFT
