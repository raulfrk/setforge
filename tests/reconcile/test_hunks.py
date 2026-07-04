"""Unit tests for the A5 per-hunk staging model (:mod:`setforge.reconcile.hunks`).

Covers hunk extraction (base↔live 2-way diff), identity (EOL-normalised content
hash + base-context anchor), classification carry-over, reconstruction of the
shared-promotion, and the INV-8 stage-fidelity assertion.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
    tracked = reconstruct(BASE, LIVE, hunks, {})
    assert b"## Shell" in tracked  # SHARED promoted
    assert b"Prefer zsh." in tracked
    assert b"workdir: /home/generic" in tracked  # LOCAL kept base, NOT live
    assert b"workdir: /home/raul" not in tracked


def test_reconstruct_does_not_promote_a_changed_shared_hunk() -> None:
    # A SHARED hunk flagged `changed` (anchor matched but content differs — a
    # value edit OR an anchor collision with an unstaged region) must NOT have
    # its live bytes promoted into tracked, else a never-staged region leaks.
    from setforge.reconcile.hunks import reconstruct

    hunks = _stage(BASE, LIVE, {"## Shell": HunkClass.SHARED})
    shell = next(h for h in hunks if h.label == "## Shell")
    changed = [h if h.label != "## Shell" else _replace_changed(h, True) for h in hunks]
    out = reconstruct(BASE, LIVE, changed, {})
    assert b"## Shell" not in out  # changed-SHARED held at base, not promoted
    # sanity: the same hunk without the changed flag DOES promote
    assert b"## Shell" in reconstruct(BASE, LIVE, hunks, {})
    assert shell.cls is HunkClass.SHARED  # (guards the fixture intent)


def _replace_changed(hunk: Hunk, changed: bool) -> Hunk:
    from dataclasses import replace

    return replace(hunk, changed=changed)


def test_reconstruct_all_pending_equals_base() -> None:
    from setforge.reconcile.hunks import reconstruct

    hunks = _stage(BASE, LIVE, {})  # all PENDING
    assert reconstruct(BASE, LIVE, hunks, {}) == BASE


def test_reconstruct_demote_reverts_region_to_base() -> None:
    from setforge.reconcile.hunks import reconstruct

    shared = _stage(BASE, LIVE, {"## Host paths": HunkClass.SHARED})
    assert b"workdir: /home/raul" in reconstruct(BASE, LIVE, shared, {})
    demoted = _stage(BASE, LIVE, {"## Host paths": HunkClass.LOCAL})
    out = reconstruct(BASE, LIVE, demoted, {})
    assert b"workdir: /home/raul" not in out  # demote un-captures
    assert b"workdir: /home/generic" in out


# --------------------------------------------------------------------------- #
# assert_stage_fidelity (INV-8)
# --------------------------------------------------------------------------- #


def test_fidelity_passes_for_correct_tracked() -> None:
    from setforge.reconcile.hunks import reconstruct

    hunks = _stage(BASE, LIVE, {"## Shell": HunkClass.SHARED})
    tracked = reconstruct(BASE, LIVE, hunks, {})
    assert_stage_fidelity(BASE, LIVE, tracked, hunks, {})  # no raise


def test_fidelity_raises_when_local_bytes_leak_into_tracked() -> None:
    hunks = _stage(BASE, LIVE, {"## Host paths": HunkClass.LOCAL})
    leaked = LIVE  # tracked == full live → a LOCAL hunk leaked
    with pytest.raises(InvariantViolation, match="INV-8"):
        assert_stage_fidelity(BASE, LIVE, leaked, hunks, {})


# --------------------------------------------------------------------------- #
# reconstruct — property tests (random base/live, any bytes)
# --------------------------------------------------------------------------- #


@given(base=st.binary(max_size=48), live=st.binary(max_size=48))
def test_property_all_shared_reconstructs_live(base: bytes, live: bytes) -> None:
    from setforge.reconcile.hunks import reconstruct

    hunks = [replace(h, cls=HunkClass.SHARED) for h in extract_hunks(base, live)]
    assert reconstruct(base, live, hunks, {}) == live  # promoting every hunk == live


@given(base=st.binary(max_size=48), live=st.binary(max_size=48))
def test_property_all_pending_reconstructs_base(base: bytes, live: bytes) -> None:
    from setforge.reconcile.hunks import reconstruct

    hunks = extract_hunks(base, live)  # all PENDING (the extract default)
    assert reconstruct(base, live, hunks, {}) == base  # promoting nothing == base


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
    # a non-drafted hunk omits draft_hash (rows stay byte-stable).
    assert all("draft_hash" not in row for row in rows)


# --------------------------------------------------------------------------- #
# A5c: SHARED_DRAFTED carries draft_hash through serialize + classify
# --------------------------------------------------------------------------- #


def test_serialize_emits_draft_hash_for_shared_drafted() -> None:
    (hunk,) = extract_hunks(b"alpha\nbeta\n", b"alpha\nBETA\n")
    drafted = replace(hunk, cls=HunkClass.SHARED_DRAFTED, draft_hash="sha256:dd")
    (row,) = serialize([drafted])
    assert row["cls"] == "shared_drafted"
    assert row["draft_hash"] == "sha256:dd"


def test_serialize_emits_reloc_anchor_when_set() -> None:
    (hunk,) = extract_hunks(b"alpha\nbeta\n", b"alpha\nBETA\n")
    relocatable = replace(hunk, reloc_anchor="## Foo")
    (row,) = serialize([relocatable])
    assert row["reloc_anchor"] == "## Foo"


def test_serialize_omits_reloc_anchor_when_absent() -> None:
    # omit-when-None so legacy rows stay byte-stable (mirrors draft_hash).
    (hunk,) = extract_hunks(b"alpha\nbeta\n", b"alpha\nBETA\n")
    (row,) = serialize([hunk])
    assert "reloc_anchor" not in row


def test_classify_carries_draft_hash_for_drafted_row() -> None:
    fresh = extract_hunks(BASE, LIVE)
    shell = _by_label(fresh)["## Shell"]
    stored: list[dict[str, object]] = [
        {
            "cls": HunkClass.SHARED_DRAFTED.value,
            "label": shell.label,
            "live_hash": shell.live_hash,
            "anchor": shell.anchor,
            "draft_hash": "sha256:dd",
        }
    ]
    out = _by_label(classify(fresh, stored))["## Shell"]
    assert out.cls is HunkClass.SHARED_DRAFTED
    assert out.draft_hash == "sha256:dd"


def _drafted(label: str) -> Hunk:
    h = _by_label(extract_hunks(BASE, LIVE))[label]
    return replace(h, cls=HunkClass.SHARED_DRAFTED, draft_hash="sha256:dd")


def test_reconstruct_splices_draft_not_live_not_base() -> None:
    from setforge.reconcile.hunks import reconstruct

    h = _drafted("## Host paths")
    draft = b"workdir: $HOME\n"  # the shareable rewrite
    out = reconstruct(BASE, LIVE, [h], {h.anchor: draft})
    assert b"workdir: $HOME" in out  # the DRAFT is promoted into tracked
    assert b"workdir: /home/raul" not in out  # NOT live (host bytes stay local)
    assert b"workdir: /home/generic" not in out  # NOT base


def test_reconstruct_drafted_independent_of_live_drift() -> None:
    # The drop regression: a SHARED_DRAFTED region must reconstruct to its draft
    # regardless of the live bytes — a later live edit cannot drop the draft.
    from setforge.reconcile.hunks import reconstruct

    draft = b"workdir: $HOME\n"
    live2 = LIVE.replace(b"workdir: /home/raul", b"workdir: /home/somewhere-else")
    # rebuild the hunk against the drifted live so its spans are valid
    h2 = replace(
        _by_label(extract_hunks(BASE, live2))["## Host paths"],
        cls=HunkClass.SHARED_DRAFTED,
        draft_hash="sha256:dd",
    )
    out = reconstruct(BASE, live2, [h2], {h2.anchor: draft})
    assert b"workdir: $HOME" in out
    assert b"somewhere-else" not in out


def test_reconstruct_missing_draft_raises() -> None:
    from setforge.reconcile.hunks import reconstruct

    h = _drafted("## Host paths")
    with pytest.raises(InvariantViolation, match="no draft"):
        reconstruct(BASE, LIVE, [h], {})  # dangling draft pointer → fail-closed


def test_classify_drafted_anchor_collision_fails_safe() -> None:
    # Two fresh hunks share one anchor; a stored SHARED_DRAFTED row for it must NOT
    # mark BOTH as non-changed SHARED_DRAFTED (which would splice the single draft
    # into both regions). The uniqueness guard makes a collision fall through to the
    # safe moved-path (changed=True → held at base in reconstruct), never duplicated.
    dup = "sha256:dup-anchor"
    fresh = [
        Hunk(HunkClass.PENDING, "one", "sha256:l1", dup, (0, 1), (0, 1)),
        Hunk(HunkClass.PENDING, "two", "sha256:l2", dup, (2, 3), (2, 3)),
    ]
    stored: list[dict[str, object]] = [
        {
            "cls": HunkClass.SHARED_DRAFTED.value,
            "label": "one",
            "live_hash": "sha256:stale",
            "anchor": dup,
            "draft_hash": "sha256:d",
        }
    ]
    out = classify(fresh, stored)
    # no hunk is splice-eligible (non-changed SHARED_DRAFTED) → no duplication
    assert not any(h.cls is HunkClass.SHARED_DRAFTED and not h.changed for h in out)


# --------------------------------------------------------------------------- #
# A2: mint + match a host-local reloc_anchor across a base restructure
# --------------------------------------------------------------------------- #

# A host-local ADDITIVE "## My Tweaks" section, first under one base (BASE_R1),
# then carried across an upstream restructure that renames the SURROUNDING base
# headings (BASE_R2) — which shifts the hunk's context anchor and breaks the
# exact/anchor identity match, leaving only the heading-identity fallback.
BASE_R1 = b"## Alpha\naaa\n## Beta\nbbb\n"
LIVE_R1 = b"## Alpha\naaa\n## My Tweaks\nmy custom line\n## Beta\nbbb\n"
BASE_R2 = b"## Gamma\nggg\n## Delta\nddd\n"
LIVE_R2 = b"## Gamma\nggg\n## My Tweaks\nmy custom line\n## Delta\nddd\n"


def _local_section() -> Hunk:
    """The extracted "## My Tweaks" insert, classified LOCAL (as if staged)."""
    hunk = _by_label(extract_hunks(BASE_R1, LIVE_R1))["## My Tweaks"]
    return replace(hunk, cls=HunkClass.LOCAL)


def test_serialize_mints_reloc_anchor_for_local_section() -> None:
    # (a) A LOCAL additive section hunk gets its heading minted as reloc_anchor.
    (row,) = serialize([_local_section()])
    assert row["cls"] == HunkClass.LOCAL.value
    assert row["reloc_anchor"] == "## My Tweaks"


def test_serialize_no_reloc_anchor_for_headingless_local_hunk() -> None:
    # (d) A LOCAL hunk whose own content carries no clean heading gets none.
    (plain,) = extract_hunks(b"alpha\nbeta\ngamma\n", b"alpha\nBETA-EDITED\ngamma\n")
    assert plain.label == "BETA-EDITED"  # first-line fallback, not a heading
    (row,) = serialize([replace(plain, cls=HunkClass.LOCAL)])
    assert "reloc_anchor" not in row


def test_classify_reloc_match_carries_local_across_restructure() -> None:
    # (b) The stored row missed by (live_hash, anchor) AND by anchor-alone, but its
    # heading identity uniquely matches → carry LOCAL, flagged changed=True.
    stored_hunk = _local_section()
    (row,) = serialize([stored_hunk])  # minted reloc_anchor persisted
    fresh = extract_hunks(BASE_R2, LIVE_R2)
    section = _by_label(fresh)["## My Tweaks"]
    # precondition: neither exact identity nor anchor-alone can match anymore.
    assert (section.live_hash, section.anchor) != (
        stored_hunk.live_hash,
        stored_hunk.anchor,
    )
    assert section.anchor != stored_hunk.anchor
    out = _by_label(classify(fresh, [row]))["## My Tweaks"]
    assert out.cls is HunkClass.LOCAL  # class carried via heading identity
    assert out.changed is True  # context shifted → surfaced for re-confirm
    assert out.reloc_anchor == "## My Tweaks"  # minted identity carried forward


def test_classify_reloc_ambiguous_stored_stays_pending() -> None:
    # (c) Two stored LOCAL rows share the heading → ambiguous → NO match, PENDING.
    h = "## My Tweaks"
    fresh = [Hunk(HunkClass.PENDING, h, "sha256:l1", "sha256:fa", (0, 0), (0, 2))]
    stored: list[dict[str, object]] = [
        {
            "cls": HunkClass.LOCAL.value,
            "label": "## My Tweaks",
            "live_hash": "sha256:s1",
            "anchor": "sha256:a1",
            "reloc_anchor": "## My Tweaks",
        },
        {
            "cls": HunkClass.LOCAL.value,
            "label": "## My Tweaks",
            "live_hash": "sha256:s2",
            "anchor": "sha256:a2",
            "reloc_anchor": "## My Tweaks",
        },
    ]
    (out,) = classify(fresh, stored)
    assert out.cls is HunkClass.PENDING  # ambiguity → safe fallthrough
    assert out.reloc_anchor is None


def test_classify_reloc_ambiguous_fresh_stays_pending() -> None:
    # (c) Two fresh hunks share the heading → ambiguous → NO match, no raise.
    h = "## My Tweaks"
    fresh = [
        Hunk(HunkClass.PENDING, h, "sha256:l1", "sha256:fa", (0, 0), (0, 2)),
        Hunk(HunkClass.PENDING, h, "sha256:l2", "sha256:fb", (4, 4), (6, 8)),
    ]
    stored: list[dict[str, object]] = [
        {
            "cls": HunkClass.LOCAL.value,
            "label": "## My Tweaks",
            "live_hash": "sha256:s1",
            "anchor": "sha256:a1",
            "reloc_anchor": "## My Tweaks",
        }
    ]
    out = classify(fresh, stored)
    assert all(h.cls is HunkClass.PENDING for h in out)  # neither picked


def test_classify_headingless_hunk_does_not_reloc_match() -> None:
    # (d) A headingless hunk never matches a stored reloc_anchor (no spurious carry).
    (plain,) = extract_hunks(b"alpha\nbeta\ngamma\n", b"alpha\nBETA-EDITED\ngamma\n")
    stored: list[dict[str, object]] = [
        {
            "cls": HunkClass.LOCAL.value,
            "label": "## My Tweaks",
            "live_hash": "sha256:s1",
            "anchor": "sha256:a1",
            "reloc_anchor": "## My Tweaks",
        }
    ]
    (out,) = classify([plain], stored)
    assert out.cls is HunkClass.PENDING
    assert out.reloc_anchor is None
