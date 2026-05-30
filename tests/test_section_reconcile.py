"""Tests for the install-side three-way section reconciler."""

import hashlib
from pathlib import Path

import pytest

from setforge.errors import MarkerError
from setforge.section_reconcile import (
    SectionDrift,
    SectionDriftState,
    _classify_one,
    classify_section_drift,
    has_shared_drift,
    maintain_marker_hashes,
    stamp_tracked_baseline,
)
from setforge.sections import (
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
        f"<!-- setforge:user-section start {semantics} {name} -->\n"
        f"{body}"
        f"<!-- setforge:user-section end {semantics} {name}{hash_segment} -->\n"
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


def test_classify_section_drift_strict_tracked_rejects_hashless_tracked() -> None:
    """Post-9ln, the classifier parses tracked strictly — hashless tracked
    is unreachable in steady state and surfaces as :class:`MarkerError`.

    The LEGACY drift state is preserved (see
    ``test_classify_section_drift_legacy_when_live_hashless``) but it now
    originates only from the install path's legacy-live migration, never
    from tracked-side gaps.
    """
    body = "rule A\n"
    tracked_text = _make_text("workflow", "shared", body, None)
    live_text = _make_text("workflow", "shared", body + "live edit\n", _sha256(body))
    with pytest.raises(MarkerError, match="missing required 'hash="):
        classify_section_drift(tracked_text, live_text)


def test_classify_section_drift_raises_on_parser_key_set_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A section present in both bodies but absent from a section primitive
    surfaces as a clear MarkerError, not a raw KeyError or a silent default.

    Simulate a parser key-set disagreement by stubbing one primitive
    (``section_semantics``) so it omits a key that ``extract_sections``
    reports on both sides. The classifier must raise a domain error that
    names the offending primitive, never mask the drift.
    """
    import setforge.section_reconcile as sr

    body = "shared body\n"
    digest = _sha256(body)
    tracked = _make_text("workflow", "shared", body, digest)
    live = _make_text("workflow", "shared", body, digest)
    monkeypatch.setattr(sr, "section_semantics", lambda _text: {})
    with pytest.raises(MarkerError, match="semantics_map"):
        classify_section_drift(tracked, live)
    # The error must NOT be a bare KeyError leaking through.
    monkeypatch.setattr(sr, "hash_sections", lambda _t, **_k: {})
    with pytest.raises(MarkerError):
        classify_section_drift(tracked, live)


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
    tracked = "<!-- setforge:user-section start workflow -->\nbody\n"
    live = ""
    with pytest.raises(MarkerError):
        classify_section_drift(tracked, live)


def test_classify_section_drift_legacy_live_returns_LEGACY_state() -> None:
    """Migration scenario: pre-9by live (no semantics
    keyword, no end-marker hash) + stamped tracked passes through the
    classifier's ``allow_legacy=True`` live-side path and yields the
    :attr:`SectionDriftState.LEGACY` state for every section. The
    state stays reachable so the wizard's two-way keep-live fallback
    still fires on first install."""
    body = "rule A\n"
    tracked_text = _make_text("workflow", "shared", body, _sha256(body))
    live_text = (
        "<!-- setforge:user-section start workflow -->\n"
        f"{body}live edit\n"
        "<!-- setforge:user-section end workflow -->\n"
    )
    result = classify_section_drift(tracked_text, live_text)
    assert result["workflow"].state is SectionDriftState.LEGACY
    # Live semantics were untagged — classifier reports tracked's semantics.
    assert result["workflow"].semantics is SectionSemantics.SHARED


# ---------------------------------------------------------------------------
# maintain_marker_hashes — invariant
# ---------------------------------------------------------------------------


def test_maintain_marker_hashes_writes_body_hashes_into_markers() -> None:
    """Post-9ln, ``maintain_marker_hashes`` runs over output of
    :func:`merge_sections`, which copies tracked's hash-stamped markers
    verbatim. A stale or placeholder hash is the legitimate input shape."""
    body = "rule A\nrule B\n"
    text = _make_text("workflow", "shared", body, "0" * 64)
    result = maintain_marker_hashes(text)
    assert extract_marker_hashes(result) == {"workflow": _sha256(body)}


def test_maintain_marker_hashes_replaces_stale_hash() -> None:
    body = "new body\n"
    text = _make_text("workflow", "shared", body, "deadbeef" * 8)
    result = maintain_marker_hashes(text)
    assert extract_marker_hashes(result) == {"workflow": _sha256(body)}


def test_maintain_marker_hashes_idempotent() -> None:
    body = "rule A\n"
    text = _make_text("workflow", "shared", body, "0" * 64)
    once = maintain_marker_hashes(text)
    twice = maintain_marker_hashes(once)
    assert once == twice


def test_maintain_marker_hashes_invariant_extracted_equals_computed() -> None:
    """Post-install invariant: extract_marker_hashes == hash_sections."""
    text = _make_text("workflow", "shared", "body\n", "0" * 64) + _make_text(
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
    # Start from a hashless legacy base, but plumb allow_legacy=True since
    # set_marker_hashes is what stamps the proper hashes — the post-stamp
    # round-trip below goes through the strict path.
    base = _make_text("workflow", "shared", body, None)
    tracked = set_marker_hashes(
        base, hash_sections(base, allow_legacy=True), allow_legacy=True
    )
    live = set_marker_hashes(
        base, hash_sections(base, allow_legacy=True), allow_legacy=True
    )
    result = classify_section_drift(tracked, live)
    assert result["workflow"].state is SectionDriftState.NO_DRIFT


# ---------------------------------------------------------------------------
# Group A: classify_section_drift -> INCONSISTENT (mid-migration shapes).
# ---------------------------------------------------------------------------


def test_classify_section_drift_yields_inconsistent_on_each_side_pristine() -> None:
    """Both sides are baseline-stamped against their own body, but the
    bodies disagree. This hits the ``_classify_one`` fall-through:

    - ``a_t != a_l`` (bodies differ, so hashes differ) -> not NO_DRIFT.
    - ``e_t`` and ``e_l`` are both present -> not LEGACY.
    - ``a_l == e_l`` (live pristine) AND ``a_t == e_t`` (tracked pristine)
      -> none of PENDING_TRACKED / LIVE_EDITED / CONFLICT matches.
    - Falls through to INCONSISTENT.

    Shouldn't happen in steady state (hashes agree on each side but
    bodies disagree across sides), but the reconciler must classify
    it deterministically rather than crash.
    """
    tracked_body = "tracked content\n"
    live_body = "live content\n"
    tracked_text = _make_text("workflow", "shared", tracked_body, _sha256(tracked_body))
    live_text = _make_text("workflow", "shared", live_body, _sha256(live_body))
    result = classify_section_drift(tracked_text, live_text)
    assert result["workflow"].state is SectionDriftState.INCONSISTENT


# ---------------------------------------------------------------------------
# Group B: stamp_tracked_baseline no-op + write paths + body preservation.
# ---------------------------------------------------------------------------


def test_stamp_tracked_baseline_returns_false_when_aligned(tmp_path: Path) -> None:
    """Already-stamped tracked file: no-op (returns False, byte-identical)."""
    body = "BODY_BYTES\n"
    aligned = (
        "header before\n"
        + _make_text("NAME", "shared", body, _sha256(body))
        + "trailer after\n"
    )
    path = tmp_path / "tracked.md"
    path.write_text(aligned, encoding="utf-8")
    assert stamp_tracked_baseline(path) is False
    assert path.read_text(encoding="utf-8") == aligned


def test_stamp_tracked_baseline_returns_true_when_unaligned(tmp_path: Path) -> None:
    """Stale-hash tracked: rewrites file, returns True, hash matches body.

    ``stamp_tracked_baseline`` parses tracked strictly (no
    ``allow_legacy``), so the unaligned fixture must already carry a
    syntactically valid ``hash=<...>`` segment whose value disagrees
    with the actual body hash.
    """
    body = "DIFFERENT_BYTES\n"
    stale = _make_text("NAME", "shared", body, "0" * 64)
    path = tmp_path / "tracked.md"
    path.write_text(stale, encoding="utf-8")
    assert stamp_tracked_baseline(path) is True
    rewritten = path.read_text(encoding="utf-8")
    assert f"hash={_sha256(body)}" in rewritten
    assert "DIFFERENT_BYTES" in rewritten


def test_stamp_tracked_baseline_preserves_body_bytes_outside_end_marker(
    tmp_path: Path,
) -> None:
    """Only end-marker hash= changes; everything else byte-preserved."""
    body = "PRESERVED_BODY\n"
    stale = (
        "header line\n"
        + _make_text("NAME", "shared", body, "0" * 64)
        + "trailer line\n"
    )
    path = tmp_path / "tracked.md"
    path.write_text(stale, encoding="utf-8")
    stamp_tracked_baseline(path)
    rewritten = path.read_text(encoding="utf-8")
    # The ONLY byte that may change is the end-marker's hash= segment:
    # zeros -> sha256 of body. Asserting full byte-equality catches any
    # other accidental mutation (line endings, marker spacing, body bytes).
    expected = stale.replace("hash=" + "0" * 64, f"hash={_sha256(body)}")
    assert rewritten == expected
