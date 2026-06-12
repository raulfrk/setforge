"""Tests for the non-interactive disposition merge driver.

Covers the disposition x auto x base-presence matrix that
:func:`setforge.disposition_merge.resolve_file` maps to deployed text and a
re-baseline decision, across markdown / YAML / JSONC formats.
"""

import io
from pathlib import Path

import pytest
from json5.loader import loads as _json5_loads
from ruamel.yaml import YAML

from setforge.config import Disposition
from setforge.disposition_merge import (
    FileResolution,
    StructuralSpanOrphan,
    StructuralSpanOrphanReason,
    exclude_structural_spans_for_capture,
    resolve_file,
    validate_structural_span_overlap,
)
from setforge.errors import ConfigError
from setforge.markdown_merge import LineConflict
from setforge.section_wizard import ReconcileAuto
from setforge.spans import SpanEntry, SpanKind
from setforge.structural_merge import PathConflict, get_at_path


def _yaml_load(text: str) -> object:
    """Parse YAML text to a plain python value for assertion."""
    return YAML(typ="safe").load(text)


# ---------------------------------------------------------------------------
# 1. PINNED.
# ---------------------------------------------------------------------------


def test_pinned_returns_live_verbatim() -> None:
    live = "alpha\nbeta\n"
    res = resolve_file(
        Disposition.PINNED,
        Path("note.md"),
        base="old\n",
        live=live,
        tracked="tracked\n",
        auto=None,
    )
    assert res == FileResolution(
        text=live, conflicts=[], advance_base=False, base_absent=False
    )


def test_pinned_live_absent_first_install_deploys_tracked() -> None:
    """Fresh host: no live file → PINNED deploys tracked once, no base advance.

    The caller passes live="" as a placeholder when the destination does
    not exist; without the live_absent flag the pin would deploy an EMPTY
    file. Every later run sees a live file and returns it untouched.
    """
    res = resolve_file(
        Disposition.PINNED,
        Path("settings.json"),
        base=None,
        live="",
        tracked='{"a": 1}\n',
        auto=None,
        live_absent=True,
    )
    assert res == FileResolution(
        text='{"a": 1}\n', conflicts=[], advance_base=False, base_absent=False
    )


def test_pinned_live_present_unchanged_by_live_absent_default() -> None:
    """The shipped pinned behavior is untouched when a live file exists."""
    res = resolve_file(
        Disposition.PINNED,
        Path("settings.json"),
        base=None,
        live="live\n",
        tracked="tracked\n",
        auto=None,
    )
    assert res.text == "live\n"
    assert res.advance_base is False


def test_pinned_ignores_yaml_and_jsonc() -> None:
    live = "a: 1\n"
    res = resolve_file(
        Disposition.PINNED,
        Path("c.yaml"),
        base="a: 0\n",
        live=live,
        tracked="a: 9\n",
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert res.text == live
    assert res.advance_base is False


# ---------------------------------------------------------------------------
# 2. base absent -> 2-way fallback (deploy tracked verbatim).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tracked"),
    [
        ("note.md", "line one\nline two\n"),
        ("conf.yaml", "key: value\n"),
        ("settings.json", '{\n  "k": 1\n}\n'),
    ],
)
def test_base_absent_deploys_tracked(name: str, tracked: str) -> None:
    res = resolve_file(
        Disposition.SHARED,
        Path(name),
        base=None,
        live="something different\n",
        tracked=tracked,
        auto=None,
    )
    assert res.text == tracked
    assert res.conflicts == []
    assert res.advance_base is True
    assert res.base_absent is True


# ---------------------------------------------------------------------------
# 3. clean merge: non-overlapping changes on both sides.
# ---------------------------------------------------------------------------


def test_shared_markdown_clean_non_overlapping() -> None:
    base = "title\nbody\nfooter\n"
    live = "TITLE\nbody\nfooter\n"  # changed first line
    tracked = "title\nbody\nFOOTER\n"  # changed last line
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts == []
    assert res.advance_base is True
    assert res.base_absent is False
    assert "TITLE" in res.text
    assert "FOOTER" in res.text


def test_shared_yaml_clean_non_overlapping() -> None:
    base = "a: 1\nb: 2\n"
    live = "a: 10\nb: 2\n"  # changed a
    tracked = "a: 1\nb: 20\n"  # changed b
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts == []
    assert res.advance_base is True
    loaded = _yaml_load(res.text)
    assert loaded == {"a": 10, "b": 20}


def test_shared_jsonc_clean_non_overlapping() -> None:
    base = '{\n  "a": 1,\n  "b": 2\n}\n'
    live = '{\n  "a": 10,\n  "b": 2\n}\n'
    tracked = '{\n  "a": 1,\n  "b": 20\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts == []
    assert res.advance_base is True
    assert _json5_loads(res.text) == {"a": 10, "b": 20}


# ---------------------------------------------------------------------------
# 4. conflict + KEEP_LIVE -> ours wins; advance_base True.
# ---------------------------------------------------------------------------


def test_shared_markdown_conflict_keep_live() -> None:
    base = "value\n"
    live = "live-value\n"
    tracked = "tracked-value\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.KEEP_LIVE,
    )
    assert res.conflicts
    assert all(isinstance(c, LineConflict) for c in res.conflicts)
    assert "live-value" in res.text
    assert "tracked-value" not in res.text
    assert res.advance_base is True


def test_shared_yaml_conflict_keep_live() -> None:
    base = "a: 1\n"
    live = "a: 2\n"
    tracked = "a: 3\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.KEEP_LIVE,
    )
    assert res.conflicts
    assert all(isinstance(c, PathConflict) for c in res.conflicts)
    assert _yaml_load(res.text) == {"a": 2}
    assert res.advance_base is True


def test_shared_jsonc_conflict_keep_live() -> None:
    base = '{\n  "a": 1\n}\n'
    live = '{\n  "a": 2\n}\n'
    tracked = '{\n  "a": 3\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.KEEP_LIVE,
    )
    assert res.conflicts
    assert _json5_loads(res.text) == {"a": 2}
    assert res.advance_base is True


# ---------------------------------------------------------------------------
# 5. conflict + USE_TRACKED -> theirs wins; advance_base True.
# ---------------------------------------------------------------------------


def test_shared_markdown_conflict_use_tracked() -> None:
    base = "value\n"
    live = "live-value\n"
    tracked = "tracked-value\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert res.conflicts
    assert "tracked-value" in res.text
    assert "live-value" not in res.text
    assert res.advance_base is True


def test_shared_yaml_conflict_use_tracked() -> None:
    base = "a: 1\n"
    live = "a: 2\n"
    tracked = "a: 3\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert res.conflicts
    assert _yaml_load(res.text) == {"a": 3}
    assert res.advance_base is True


def test_shared_jsonc_conflict_use_tracked() -> None:
    base = '{\n  "a": 1\n}\n'
    live = '{\n  "a": 2\n}\n'
    tracked = '{\n  "a": 3\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert res.conflicts
    assert _json5_loads(res.text) == {"a": 3}
    assert res.advance_base is True


# ---------------------------------------------------------------------------
# 6. conflict + auto None -> ours wins in text BUT advance_base False (defer).
# ---------------------------------------------------------------------------


def test_shared_markdown_conflict_bare_defers() -> None:
    base = "value\n"
    live = "live-value\n"
    tracked = "tracked-value\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts
    assert "live-value" in res.text
    assert res.advance_base is False


def test_shared_yaml_conflict_bare_defers() -> None:
    base = "a: 1\n"
    live = "a: 2\n"
    tracked = "a: 3\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts
    assert _yaml_load(res.text) == {"a": 2}
    assert res.advance_base is False


def test_shared_jsonc_conflict_bare_defers() -> None:
    base = '{\n  "a": 1\n}\n'
    live = '{\n  "a": 2\n}\n'
    tracked = '{\n  "a": 3\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts
    assert _json5_loads(res.text) == {"a": 2}
    assert res.advance_base is False


# ---------------------------------------------------------------------------
# 7. forked behaves like shared for the merge itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auto", [None, ReconcileAuto.KEEP_LIVE, ReconcileAuto.USE_TRACKED]
)
def test_forked_merge_parity_with_shared_markdown(auto: ReconcileAuto | None) -> None:
    base = "value\n"
    live = "live-value\n"
    tracked = "tracked-value\n"
    shared = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=auto,
    )
    forked = resolve_file(
        Disposition.FORKED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=auto,
    )
    assert forked.text == shared.text
    assert forked.advance_base == shared.advance_base
    assert len(forked.conflicts) == len(shared.conflicts)


def test_forked_merge_parity_with_shared_yaml() -> None:
    base = "a: 1\nb: 2\n"
    live = "a: 10\nb: 2\n"
    tracked = "a: 1\nb: 20\n"
    shared = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    forked = resolve_file(
        Disposition.FORKED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert _yaml_load(forked.text) == _yaml_load(shared.text)
    assert forked.advance_base == shared.advance_base


# ---------------------------------------------------------------------------
# 8. structural round-trip: comment on untouched key survives.
# ---------------------------------------------------------------------------


def test_shared_yaml_preserves_comment_on_untouched_key() -> None:
    base = "a: 1\nb: 2  # keep me\n"
    live = "a: 10\nb: 2  # keep me\n"
    tracked = "a: 1\nb: 2  # keep me\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert "# keep me" in res.text
    assert _yaml_load(res.text) == {"a": 10, "b": 2}


def test_shared_jsonc_preserves_comment_on_untouched_key() -> None:
    base = '{\n  "a": 1,\n  "b": 2 // keep me\n}\n'
    live = '{\n  "a": 10,\n  "b": 2 // keep me\n}\n'
    tracked = '{\n  "a": 1,\n  "b": 2 // keep me\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert "// keep me" in res.text
    assert _json5_loads(res.text) == {"a": 10, "b": 2}


# ---------------------------------------------------------------------------
# 9. shape mismatch falls back to line-based merge without raising.
# ---------------------------------------------------------------------------


def test_shape_mismatch_falls_back_to_line_merge() -> None:
    base = "a:\n  nested: 1\n"
    live = "a:\n  nested: 2\n"  # still a map, changed
    tracked = "a: scalar\n"  # now a scalar -> shape mismatch
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    # Did not raise; produced text via the line-based fallback.
    assert isinstance(res, FileResolution)
    assert isinstance(res.text, str)
    # A conflict at the diverged region; conflicts are LineConflict (line path).
    assert all(isinstance(c, LineConflict) for c in res.conflicts)


# ===========================================================================
# Structural span pinning: B-S1..B-S8 + I9/I10/I11.
# ===========================================================================


def _pin(anchor: str) -> SpanEntry:
    return SpanEntry.model_validate({"anchor": anchor, "kind": "pinned"})


def _fork(anchor: str) -> SpanEntry:
    return SpanEntry.model_validate({"anchor": anchor, "kind": "forked"})


def _yaml_value_at(text: str, path: str) -> object:
    """Parse YAML ``text`` and return the unwrapped value at dotted ``path``."""
    doc = YAML(typ="rt").load(io.StringIO(text))
    return get_at_path(doc, path)


# ---- pinned re-assert: upstream changed P, live did not (the core case) ----


def test_pinned_reasserts_live_when_upstream_changed_p() -> None:
    # base == live at P (live unchanged); tracked (theirs) changed P. Without
    # the pin, the merge auto-takes theirs. The pin re-imposes live.
    base = "editor:\n  fontSize: 12\nother: keep\n"
    live = "editor:\n  fontSize: 12\nother: keep\n"
    tracked = "editor:\n  fontSize: 99\nother: keep\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[_pin("editor.fontSize")],
    )
    assert _yaml_value_at(res.text, "editor.fontSize") == 12
    assert res.structural_span_orphans == []
    assert res.advance_base is True


def test_pinned_reassert_across_two_merge_passes_no_phantom_conflict() -> None:
    # Pass 1: live==base, tracked changed P; pin holds live; re-baseline to
    # the post-reassert dump. Pass 2: feed that dump as the new base; the pin
    # must STILL hold live with NO phantom conflict (B-S6 / I1).
    tracked = "editor:\n  fontSize: 99\nkeep: yes\n"
    live = "editor:\n  fontSize: 12\nkeep: yes\n"
    span = [_pin("editor.fontSize")]
    res1 = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=live,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=span,
    )
    assert _yaml_value_at(res1.text, "editor.fontSize") == 12
    # Re-baseline base = post-reassert dump (B-S6). Live unchanged.
    res2 = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=res1.text,
        live=res1.text,
        tracked=tracked,
        auto=None,
        structural_spans=span,
    )
    assert _yaml_value_at(res2.text, "editor.fontSize") == 12
    assert res2.conflicts == []  # no phantom conflict
    assert res2.structural_span_orphans == []


def test_rebaseline_equals_post_reassert_dump(tmp_path: Path) -> None:
    # B-S6: advance_base is True and res.text IS the post-reassert dump (the
    # caller re-baselines base := res.text). Re-running with that base yields
    # a byte-identical, conflict-free result.
    base = "a:\n  b: 1\n"
    live = "a:\n  b: 1\n"
    tracked = "a:\n  b: 2\n"
    span = [_pin("a.b")]
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=span,
    )
    assert res.advance_base is True
    # The re-baselined base is res.text; replay is byte-identical at P.
    res2 = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=res.text,
        live=res.text,
        tracked=tracked,
        auto=None,
        structural_spans=span,
    )
    assert _yaml_value_at(res2.text, "a.b") == 1
    assert res2.text == res.text


def test_pinned_jsonc_reasserts_live() -> None:
    base = '{\n  "editor": {\n    "fontSize": 12\n  }\n}\n'
    live = '{\n  "editor": {\n    "fontSize": 12\n  }\n}\n'
    tracked = '{\n  "editor": {\n    "fontSize": 99\n  }\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("c.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[_pin("editor.fontSize")],
    )
    model = _json5_loads(res.text)
    assert model["editor"]["fontSize"] == 12


# ---- pinned BOTH-sides changed: conflict suppressed, base advances ----


def test_pinned_both_sides_changed_suppresses_conflict_and_advances() -> None:
    # Live froze P (live != base) AND upstream changed the SAME P (tracked !=
    # base, tracked != live) -> merge_structural records a real PathConflict at
    # P. The pin is deterministic live-wins, so that conflict must be SUPPRESSED
    # (not deferred): the resolution carries NO conflict for P, advance_base is
    # True, P holds the live value, and a second pass is byte-stable with no
    # phantom conflict.
    base = "editor:\n  fontSize: 1\nkeep: yes\n"
    live = "editor:\n  fontSize: 12\nkeep: yes\n"
    tracked = "editor:\n  fontSize: 99\nkeep: yes\n"
    span = [_pin("editor.fontSize")]
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=span,
    )
    assert res.conflicts == []  # the pinned-path conflict is suppressed
    assert res.advance_base is True
    assert _yaml_value_at(res.text, "editor.fontSize") == 12
    # Second pass with the re-baselined base is byte-stable, no phantom conflict.
    res2 = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=res.text,
        live=res.text,
        tracked=tracked,
        auto=None,
        structural_spans=span,
    )
    assert res2.conflicts == []
    assert res2.advance_base is True
    assert res2.text == res.text


def test_pinned_conflict_suppressed_but_nonpinned_conflict_still_defers() -> None:
    # Suppression is scoped to PINNED paths only: a genuine conflict on a
    # NON-pinned path (no override) must still defer under bare auto. Here both
    # `editor.fontSize` (pinned) and `editor.tabSize` (un-pinned) conflict; only
    # the pinned one is suppressed, the un-pinned one keeps the base from
    # advancing.
    base = "editor:\n  fontSize: 1\n  tabSize: 1\n"
    live = "editor:\n  fontSize: 12\n  tabSize: 2\n"
    tracked = "editor:\n  fontSize: 99\n  tabSize: 3\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[_pin("editor.fontSize")],
    )
    assert all(isinstance(c, PathConflict) for c in res.conflicts)
    assert [c.path for c in res.conflicts if isinstance(c, PathConflict)] == [
        "editor.tabSize"
    ]
    assert res.advance_base is False
    # The pin still held live at its own path.
    assert _yaml_value_at(res.text, "editor.fontSize") == 12


# ---- forked: merges upstream (no re-assert) ----


def test_forked_takes_upstream_no_reassert() -> None:
    # A forked span path MERGES upstream normally (no re-assert): tracked
    # changed P, live did not -> the merge takes theirs.
    base = "a:\n  b: 1\n"
    live = "a:\n  b: 1\n"
    tracked = "a:\n  b: 2\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[_fork("a.b")],
    )
    assert _yaml_value_at(res.text, "a.b") == 2
    assert res.structural_span_orphans == []


# ---- whole-subtree pin: sibling comments survive, only P's subtree drops ----


def test_whole_subtree_pin_preserves_sibling_comments() -> None:
    # B-S8: a whole-subtree plain-value re-assert at `pinned` must not clobber
    # the sibling `other` key's comment tokens.
    base = "pinned:\n  x: 1\nother: keep  # sibling comment\n"
    live = "pinned:\n  x: 1\nother: keep  # sibling comment\n"
    tracked = "pinned:\n  x: 9\nother: keep  # sibling comment\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[_pin("pinned")],
    )
    # Live subtree won at `pinned`.
    assert _yaml_value_at(res.text, "pinned.x") == 1
    # Sibling comment byte-survives the subtree re-assert.
    assert "# sibling comment" in res.text


# ---- orphans: parent-type-changed, missing parent, absent-in-live ----


def test_orphan_missing_parent_keyerror_warns_not_raises() -> None:
    # B-S3: tracked removed the pinned path's parent; the merge takes that
    # deletion, so set_at_path raises KeyError on re-assert -> orphan-warn,
    # NEVER an uncaught raise. Live still has the parent.
    base = "a:\n  b: 1\nkeep: yes\n"
    live = "a:\n  b: 1\nkeep: yes\n"
    tracked = "keep: yes\n"  # removed `a` entirely
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[_pin("a.b")],
    )
    assert res.structural_span_orphans == [
        StructuralSpanOrphan(
            anchor="a.b",
            kind=SpanKind.PINNED,
            reason=StructuralSpanOrphanReason.MISSING_PARENT,
        )
    ]


def test_orphan_parent_type_changed_warns_not_raises() -> None:
    # B-S3: upstream changed the pinned path's parent from a mapping to a
    # scalar; set_at_path raises MergeTypeMismatch -> orphan-warn. Both sides
    # changed the parent's shape so the file-level fallback is NOT triggered
    # for the *pin* (the merge itself must stay structural).
    base = "a:\n  b: 1\n"
    # live keeps a as a mapping; tracked turns a into a scalar but live==base
    # at `a`, so the merge takes theirs (a -> scalar). The pin then can't
    # address a.b -> MergeTypeMismatch on re-assert.
    live = "a:\n  b: 1\n"
    tracked = "a: scalar\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[_pin("a.b")],
    )
    assert len(res.structural_span_orphans) == 1
    orphan = res.structural_span_orphans[0]
    assert orphan.anchor == "a.b"
    assert orphan.reason is StructuralSpanOrphanReason.PARENT_NOT_MAPPING


def test_orphan_absent_in_live_skips_with_warn() -> None:
    # B-S4: the user deleted P locally (absent in live) -> snapshot is ABSENT,
    # no re-assert, orphan-warn. The merged value (tracked's) stays.
    base = "a:\n  b: 1\nkeep: yes\n"
    live = "keep: yes\n"  # user deleted `a` locally
    tracked = "a:\n  b: 5\nkeep: yes\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        structural_spans=[_pin("a.b")],
    )
    assert res.structural_span_orphans == [
        StructuralSpanOrphan(
            anchor="a.b",
            kind=SpanKind.PINNED,
            reason=StructuralSpanOrphanReason.ABSENT_IN_LIVE,
        )
    ]


# ---- I10: list-index pin rejected at pin time ----


def test_list_index_pin_rejected_at_pin_time() -> None:
    # I10: a list-suffix anchor is refused UP FRONT (pin time) with a clear
    # ConfigError, not deferred to an opaque get/set failure.
    with pytest.raises(ConfigError, match="list suffix"):
        resolve_file(
            Disposition.SHARED,
            Path("c.yaml"),
            base="a:\n  - 1\n  - 2\n",
            live="a:\n  - 1\n  - 2\n",
            tracked="a:\n  - 1\n  - 9\n",
            auto=None,
            structural_spans=[_pin("a[*]")],
        )


# ---- I11 / B-S7: overlapping / nested pins rejected ----


def test_overlap_validator_rejects_prefix_nesting() -> None:
    with pytest.raises(ConfigError, match="overlapping"):
        validate_structural_span_overlap([_pin("a"), _pin("a.b")])


def test_overlap_validator_rejects_duplicate_anchor() -> None:
    with pytest.raises(ConfigError, match="overlapping"):
        validate_structural_span_overlap([_pin("a.b"), _fork("a.b")])


def test_overlap_validator_allows_disjoint_and_sibling_prefix() -> None:
    # `a.b` and `a.c` are disjoint; `ab` does not prefix `a` at segment level.
    validate_structural_span_overlap([_pin("a.b"), _pin("a.c"), _pin("ab")])


def test_resolve_file_rejects_overlapping_pins() -> None:
    with pytest.raises(ConfigError):
        resolve_file(
            Disposition.SHARED,
            Path("c.yaml"),
            base="a:\n  b: 1\n",
            live="a:\n  b: 1\n",
            tracked="a:\n  b: 2\n",
            auto=None,
            structural_spans=[_pin("a"), _pin("a.b")],
        )


# ---- capture exclusion (both kinds, B-S5 / I2 totality) ----


def test_capture_excludes_pinned_path_yaml() -> None:
    # Live changed P; capture must restore tracked's value at P (P never
    # written back), while the rest of live captures.
    live = "editor:\n  fontSize: 22\nshared: live-edit\n"
    tracked = "editor:\n  fontSize: 12\nshared: old\n"
    out = exclude_structural_spans_for_capture(
        live, tracked, [_pin("editor.fontSize")], is_jsonc=False
    )
    assert _yaml_value_at(out, "editor.fontSize") == 12  # tracked kept at P
    assert _yaml_value_at(out, "shared") == "live-edit"  # rest captured


def test_capture_excludes_forked_path_too() -> None:
    # B-S5: forked paths are ALSO excluded from capture (I2 totality).
    live = "a:\n  b: 99\nrest: live\n"
    tracked = "a:\n  b: 1\nrest: old\n"
    out = exclude_structural_spans_for_capture(
        live, tracked, [_fork("a.b")], is_jsonc=False
    )
    assert _yaml_value_at(out, "a.b") == 1  # tracked kept at forked P
    assert _yaml_value_at(out, "rest") == "live"


def test_capture_exclusion_jsonc() -> None:
    live = '{\n  "a": {\n    "b": 99\n  }\n}\n'
    tracked = '{\n  "a": {\n    "b": 1\n  }\n}\n'
    out = exclude_structural_spans_for_capture(
        live, tracked, [_pin("a.b")], is_jsonc=True
    )
    assert _json5_loads(out)["a"]["b"] == 1


def test_capture_exclusion_path_absent_in_tracked_left_as_live() -> None:
    # Tracked has no value at P -> nothing to restore; live flows through.
    live = "a:\n  b: 5\n"
    tracked = "a:\n  c: 1\n"
    out = exclude_structural_spans_for_capture(
        live, tracked, [_pin("a.b")], is_jsonc=False
    )
    assert _yaml_value_at(out, "a.b") == 5
