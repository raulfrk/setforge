"""Tests for the base-aware structural 3-way merge engine.

Covers the shape decisions (recurse vs opaque-take vs conflict vs
type-mismatch), type-aware wrapper-free equality, comment + key-order
provenance (golden-file dumps for BOTH ruamel YAML and json-five JSONC),
delete-vs-edit conflict detection, and byte-stable idempotency.
"""

import io
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from json5.dumper import ModelDumper
from json5.dumper import dumps as json5_dumps
from json5.loader import ModelLoader
from json5.loader import loads as json5_loads
from ruamel.yaml import YAML

from setforge.errors import DuplicateKeyInMergeModel, MergeTypeMismatch
from setforge.scalar_merge import ABSENT
from setforge.structural_merge import (
    JSONObject,
    PathConflict,
    StructuralMergeResult,
    _json5_inner,
    _Json5Backend,
    _RuamelBackend,
    _to_plain,
    append_key_segment,
    encode_key_segment,
    get_at_path,
    get_node_at_path,
    is_structural,
    join_key_segments,
    list_keys_at_path,
    merge_structural,
    resolve_path_prefix,
    set_at_path,
    split_key_path,
)

# --------------------------------------------------------------------------
# Helpers: ruamel + json-five round-trip loaders/dumpers for the tests.
# --------------------------------------------------------------------------


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    return y


def _yload(text: str) -> object:
    return _yaml().load(io.StringIO(text))


def _ydump(node: object) -> str:
    buf = io.StringIO()
    _yaml().dump(node, buf)
    return buf.getvalue()


def _jload(text: str) -> object:
    return json5_loads(text, loader=ModelLoader())


def _jdump(model: object) -> str:
    return json5_dumps(model, dumper=ModelDumper())


# --------------------------------------------------------------------------
# Plain-dict shape decisions (no comment backend) — the pure algorithm.
# --------------------------------------------------------------------------


def test_one_side_changed_takes_that_side() -> None:
    """ours==base -> take theirs; theirs==base -> take ours."""
    base = {"a": 1, "b": 2}
    ours = {"a": 1, "b": 99}  # ours changed b
    theirs = {"a": 7, "b": 2}  # theirs changed a
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.conflicts == []
    assert result.merged_model == {"a": 7, "b": 99}


def test_both_changed_differently_conflicts() -> None:
    """Both sides diverge from base AND from each other -> PathConflict."""
    base = {"a": 1}
    ours = {"a": 2}
    theirs = {"a": 3}
    result = merge_structural(base, ours, theirs)
    assert not result.clean
    assert result.conflicts == [PathConflict(path="a", base=1, ours=2, theirs=3)]


def test_both_changed_same_is_clean_take() -> None:
    """Both sides made the identical change -> clean take, no conflict."""
    base = {"a": 1}
    ours = {"a": 5}
    theirs = {"a": 5}
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.merged_model == {"a": 5}


def test_nested_non_overlapping_edits_automerge() -> None:
    """Disjoint edits inside a shared subtree merge via recursion."""
    base = {"outer": {"x": 1, "y": 2}}
    ours = {"outer": {"x": 10, "y": 2}}
    theirs = {"outer": {"x": 1, "y": 20}}
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.merged_model == {"outer": {"x": 10, "y": 20}}


def test_theirs_only_key_appended() -> None:
    """A key only theirs added (base ABSENT) is inserted into the result."""
    base = {"a": 1}
    ours = {"a": 1}
    theirs = {"a": 1, "c": 3}
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.merged_model == {"a": 1, "c": 3}


def test_add_add_same_value_clean() -> None:
    """Both sides added the same new key with the same value -> clean."""
    base = {"a": 1}
    ours = {"a": 1, "n": 5}
    theirs = {"a": 1, "n": 5}
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.merged_model == {"a": 1, "n": 5}


def test_add_add_diff_value_conflicts() -> None:
    """Both sides added the same new key with different values -> conflict."""
    base = {"a": 1}
    ours = {"a": 1, "n": 5}
    theirs = {"a": 1, "n": 6}
    result = merge_structural(base, ours, theirs)
    assert not result.clean
    assert result.conflicts == [PathConflict(path="n", base=ABSENT, ours=5, theirs=6)]


# --------------------------------------------------------------------------
# Delete vs edit — the union-walk pitfall (no silent data loss).
# --------------------------------------------------------------------------


def test_delete_ours_unchanged_theirs_deletes() -> None:
    """ours==base, theirs deleted the key -> key is deleted, clean."""
    base = {"a": 1, "b": 2}
    ours = {"a": 1, "b": 2}
    theirs = {"a": 1}  # deleted b
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.merged_model == {"a": 1}


def test_delete_one_side_edit_inside_other_conflicts() -> None:
    """theirs deletes a key ours edited -> PathConflict, no silent loss."""
    base = {"k": {"x": 1}}
    ours = {"k": {"x": 99}}  # edited inside k
    theirs: dict[str, object] = {}  # deleted k entirely
    result = merge_structural(base, ours, theirs)
    assert not result.clean
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.path == "k"
    assert conflict.base == {"x": 1}
    assert conflict.ours == {"x": 99}
    assert conflict.theirs is ABSENT


def test_delete_ours_edit_theirs_conflicts() -> None:
    """ours deletes a key theirs edited -> PathConflict (symmetric)."""
    base = {"k": 1}
    ours: dict[str, object] = {}  # deleted k
    theirs = {"k": 2}  # edited k
    result = merge_structural(base, ours, theirs)
    assert not result.clean
    assert result.conflicts == [PathConflict(path="k", base=1, ours=ABSENT, theirs=2)]


def test_ours_wholesale_replaced_keys_does_not_crash_dict() -> None:
    """Live dropped every base/theirs key and added an unrelated one.

    theirs == base, so each dropped key resolves to a DELETE that ours already
    satisfies. Deleting an already-absent key must be a no-op, not a crash, and
    the 3-way result keeps live's wholesale replacement.
    """
    base = {"a": 1, "b": 2}
    ours = {"z": 9}  # wholesale-replaced: dropped a/b, added z
    theirs = {"a": 1, "b": 2}  # unchanged from base
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.merged_model == {"z": 9}


def test_ours_wholesale_replaced_keys_does_not_crash_jsonc() -> None:
    """JSONC (comment-backend) variant of the wholesale-replace no-op delete.

    Exercises the json-five ``JSONObject`` backend whose ``delete`` previously
    asserted the key was present; an absent key must now be a no-op.
    """
    base = _jload('{"a": 1, "b": 2}')
    ours = _jload('{"z": 9}')
    theirs = _jload('{"a": 1, "b": 2}')
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert _jdump(result.merged_model) == '{"z": 9}'


# --------------------------------------------------------------------------
# Type-aware, wrapper-free equality in the divergence test.
# --------------------------------------------------------------------------


def test_int_float_bool_never_conflated() -> None:
    """1 / 1.0 / True are distinct in the divergence test."""
    # base=1(int); ours=True(bool); theirs=1.0(float) -> all differ -> conflict.
    base = {"a": 1}
    ours = {"a": True}
    theirs = {"a": 1.0}
    result = merge_structural(base, ours, theirs)
    assert not result.clean
    assert result.conflicts == [PathConflict(path="a", base=1, ours=True, theirs=1.0)]


def test_int_vs_float_change_is_a_real_change() -> None:
    """ours keeps base(1); theirs sets 1.0 -> theirs differs -> take 1.0."""
    base = {"a": 1}
    ours = {"a": 1}
    theirs = {"a": 1.0}
    result = merge_structural(base, ours, theirs)
    assert result.clean
    merged = result.merged_model
    assert merged == {"a": 1.0}
    assert type(merged["a"]) is float


# --------------------------------------------------------------------------
# Lists are opaque whole-values.
# --------------------------------------------------------------------------


def test_list_opaque_take_one_side() -> None:
    """A list edited only on theirs is taken whole."""
    base = {"a": [1, 2]}
    ours = {"a": [1, 2]}
    theirs = {"a": [1, 2, 3]}
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.merged_model == {"a": [1, 2, 3]}


def test_list_opaque_both_changed_conflicts() -> None:
    """Both sides changed the list differently -> conflict (no merge)."""
    base = {"a": [1]}
    ours = {"a": [1, 2]}
    theirs = {"a": [1, 3]}
    result = merge_structural(base, ours, theirs)
    assert not result.clean
    assert result.conflicts[0].path == "a"


def test_list_element_type_distinctness() -> None:
    """[1] vs [True] vs [1.0] are all distinct opaque values -> conflict."""
    base = {"a": [1]}
    ours = {"a": [True]}
    theirs = {"a": [1.0]}
    result = merge_structural(base, ours, theirs)
    assert not result.clean
    assert result.conflicts == [
        PathConflict(path="a", base=[1], ours=[True], theirs=[1.0])
    ]


# --------------------------------------------------------------------------
# True shape mismatch raises MergeTypeMismatch.
# --------------------------------------------------------------------------


def test_shape_mismatch_dict_vs_scalar_raises() -> None:
    """A key that is a mapping on one diverged side and a scalar on
    another (both differ from base) raises MergeTypeMismatch."""
    base = {"k": 0}
    ours = {"k": {"x": 1}}  # became a mapping
    theirs = {"k": 5}  # became a scalar
    with pytest.raises(MergeTypeMismatch):
        merge_structural(base, ours, theirs)


def test_shape_mismatch_list_vs_dict_raises() -> None:
    base = {"k": 0}
    ours = {"k": [1, 2]}
    theirs = {"k": {"a": 1}}
    with pytest.raises(MergeTypeMismatch):
        merge_structural(base, ours, theirs)


@pytest.mark.parametrize(
    ("base", "theirs"),
    [("base", "upstream"), (["base"], ["upstream"])],
)
def test_root_mapping_ours_rejects_non_mapping_other_sides(
    base: object, theirs: object
) -> None:
    with pytest.raises(MergeTypeMismatch):
        merge_structural(base, {"local": "keep"}, theirs)


# --------------------------------------------------------------------------
# ruamel YAML: comment + key-order golden-file assertions.
# --------------------------------------------------------------------------


def test_yaml_clean_merge_preserves_comments_and_order() -> None:
    """A clean YAML merge keeps live's key order and comments; a
    TAKE-theirs scalar brings upstream's comment with it."""
    base = _yload("a: 1  # base a\nb: 2  # base b\n")
    ours = _yload("a: 1  # ours a\nb: 99  # ours b\n")  # ours changed b
    theirs = _yload("a: 7  # theirs a\nb: 2  # theirs b\n")  # theirs changed a
    result = merge_structural(base, ours, theirs)
    assert result.clean
    # a taken from theirs (with theirs' comment); b taken from ours.
    expected = "a: 7  # theirs a\nb: 99  # ours b\n"
    assert _ydump(result.merged_model) == expected


def test_yaml_theirs_only_key_brings_its_comment() -> None:
    """A theirs-only added key lands with upstream's attached comment."""
    base = _yload("a: 1  # a\n")
    ours = _yload("a: 1  # a\n")
    theirs = _yload("a: 1  # a\nc: 3  # theirs c\n")
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert _ydump(result.merged_model) == "a: 1  # a\nc: 3  # theirs c\n"


def test_yaml_nested_recursion_preserves_structure() -> None:
    """Disjoint nested edits merge and the block comment survives."""
    base = _yload("outer:\n  x: 1\n  y: 2\n")
    ours = _yload("outer:\n  x: 10  # ours x\n  y: 2\n")
    theirs = _yload("outer:\n  x: 1\n  y: 20  # theirs y\n")
    result = merge_structural(base, ours, theirs)
    assert result.clean
    expected = "outer:\n  x: 10  # ours x\n  y: 20  # theirs y\n"
    assert _ydump(result.merged_model) == expected


def test_yaml_idempotent_no_op() -> None:
    """A second merge with unchanged live/upstream is byte-stable."""
    base = _yload("a: 1  # a\nb: 2  # b\n")
    ours = _yload("a: 1  # a\nb: 99  # ours b\n")
    theirs = _yload("a: 7  # theirs a\nb: 2  # b\n")
    first = merge_structural(base, ours, theirs)
    first_dump = _ydump(first.merged_model)
    # Re-merge the merged result against the same theirs, with the merged
    # output now serving as both base and ours: should be a clean no-op.
    base2 = _yload(first_dump)
    ours2 = _yload(first_dump)
    theirs2 = _yload(first_dump)
    second = merge_structural(base2, ours2, theirs2)
    assert second.clean
    assert second.conflicts == []
    assert _ydump(second.merged_model) == first_dump


# --------------------------------------------------------------------------
# json-five JSONC: comment + key-order golden-file assertions.
# --------------------------------------------------------------------------


def test_jsonc_clean_merge_preserves_comments_and_order() -> None:
    """A clean JSONC merge keeps live's order; a TAKE-theirs on the last key
    brings the upstream trailing comment (its ``wsc_after``) with the winning
    value, while a TAKE-ours key keeps live's comment.

    The taken-from-theirs key sits LAST so its trailing comment lives in the
    value node's ``wsc_after`` (clean value-node provenance). A non-last key's
    trailing comment is structurally bound to the FOLLOWING key in json-five,
    so this test exercises the position where provenance is well-defined.
    """
    # ours changes a (kept); theirs changes b (taken, last key).
    base = _jload('{\n  "a": 1, // base a\n  "b": 2 // base b\n}')
    ours = _jload('{\n  "a": 99, // ours a\n  "b": 2 // ours b\n}')
    theirs = _jload('{\n  "a": 1, // theirs a\n  "b": 7 // theirs b\n}')
    result = merge_structural(base, ours, theirs)
    assert result.clean
    expected = '{\n  "a": 99, // ours a\n  "b": 7 // theirs b\n}'
    assert _jdump(result.merged_model) == expected


def test_jsonc_nested_recursion() -> None:
    """Disjoint nested JSONC edits merge via recursion.

    ``x`` (changed by ours, kept) is non-last; ``y`` (changed by theirs,
    taken) is last so its trailing comment rides its value node's
    ``wsc_after``.
    """
    base = _jload('{\n  "o": {\n    "x": 1,\n    "y": 2 // base y\n  }\n}')
    ours = _jload('{\n  "o": {\n    "x": 10,\n    "y": 2 // ours y\n  }\n}')
    theirs = _jload('{\n  "o": {\n    "x": 1,\n    "y": 20 // theirs y\n  }\n}')
    result = merge_structural(base, ours, theirs)
    assert result.clean
    expected = '{\n  "o": {\n    "x": 10,\n    "y": 20 // theirs y\n  }\n}'
    assert _jdump(result.merged_model) == expected


def test_jsonc_idempotent_no_op() -> None:
    """A second JSONC merge with unchanged sides is byte-stable, and the
    appended/taken key survives a dump+reparse with its comment."""
    base = _jload('{\n  "a": 1, // base a\n  "b": 2 // base b\n}')
    ours = _jload('{\n  "a": 1, // ours a\n  "b": 99 // ours b\n}')
    theirs = _jload('{\n  "a": 7, // theirs a\n  "b": 2 // theirs b\n}')
    first = merge_structural(base, ours, theirs)
    first_dump = _jdump(first.merged_model)
    second = merge_structural(
        _jload(first_dump), _jload(first_dump), _jload(first_dump)
    )
    assert second.clean
    assert second.conflicts == []
    assert _jdump(second.merged_model) == first_dump


def test_jsonc_int_float_bool_distinct() -> None:
    """The wrapper-free divergence test keeps 1/1.0/True distinct for
    json-five nodes too."""
    base = _jload('{"a": 1}')
    ours = _jload('{"a": true}')
    theirs = _jload('{"a": 1.0}')
    result = merge_structural(base, ours, theirs)
    assert not result.clean
    assert result.conflicts == [PathConflict(path="a", base=1, ours=True, theirs=1.0)]


def test_result_is_dataclass_shape() -> None:
    """merge_structural returns a StructuralMergeResult with the documented
    attributes."""
    result = merge_structural({"a": 1}, {"a": 1}, {"a": 1})
    assert isinstance(result, StructuralMergeResult)
    assert result.merged_model == {"a": 1}
    assert isinstance(result.conflicts, list)


# --------------------------------------------------------------------------
# get_at_path: unwrapped, deep-copied snapshot seam for structural pins.
# --------------------------------------------------------------------------


def test_get_at_path_scalar_leaf() -> None:
    assert get_at_path({"a": {"b": 3}}, "a.b") == 3


def test_get_at_path_whole_subtree() -> None:
    assert get_at_path({"a": {"b": {"c": 1}}}, "a.b") == {"c": 1}


def test_get_at_path_absent_missing_leaf_is_sentinel() -> None:
    assert get_at_path({"a": {"b": 1}}, "a.z") is ABSENT


def test_get_at_path_absent_missing_parent_is_sentinel() -> None:
    assert get_at_path({"a": 1}, "x.y.z") is ABSENT


def test_get_at_path_present_null_distinct_from_absent() -> None:
    # A present null must NOT collapse to the ABSENT sentinel (B-S4).
    snap = get_at_path({"a": {"b": None}}, "a.b")
    assert snap is None
    assert snap is not ABSENT


def test_get_at_path_rejects_list_suffix() -> None:
    with pytest.raises(ValueError, match="list suffix"):
        get_at_path({"a": [1, 2]}, "a[*]")


def test_get_at_path_snapshot_is_deep_copy_plain_dict() -> None:
    # B-S1/B-S2: a snapshot must survive a later in-place mutation of source.
    model = {"a": {"b": {"c": 1}}}
    snap = get_at_path(model, "a.b")
    model["a"]["b"]["c"] = 999
    assert snap == {"c": 1}


def test_get_at_path_snapshot_is_deep_copy_yaml() -> None:
    # The snapshot from a ruamel CommentedMap must be an unwrapped plain value,
    # NOT a held node alias — mutating the live model after the snapshot must
    # not clobber it (B-S1).
    model = cast("dict[str, dict[str, int]]", _yload("a:\n  b: 1  # c\n"))
    snap = get_at_path(model, "a")
    assert snap == {"b": 1}
    # Mutate the live model in place; the snapshot must be unaffected.
    model["a"]["b"] = 42
    assert snap == {"b": 1}


def test_get_at_path_then_merge_does_not_clobber_snapshot_jsonc() -> None:
    # End-to-end B-S1: snapshot a json-five subtree, then run a merge that
    # mutates ours in place toward theirs; the snapshot stays the live value.
    base = _jload('{"a": 1}')
    ours = _jload('{"a": 1}')
    theirs = _jload('{"a": 2}')
    snap = get_at_path(ours, "a")
    assert snap == 1
    merge_structural(base, ours, theirs)
    assert get_at_path(ours, "a") == 2  # merge took theirs
    assert snap == 1  # snapshot untouched


# --------------------------------------------------------------------------
# resolve_path_prefix: deepest-resolvable-prefix navigation beside get_at_path.
# --------------------------------------------------------------------------


def test_resolve_path_prefix_present_path() -> None:
    assert resolve_path_prefix({"a": {"b": {"c": 1}}}, "a.b.c") == ("a.b.c", None)


def test_resolve_path_prefix_leaf_missing() -> None:
    assert resolve_path_prefix({"a": {"b": 1}}, "a.z") == ("a", "a.z")


def test_resolve_path_prefix_mid_path_missing() -> None:
    assert resolve_path_prefix({"a": {"b": 1}}, "a.x.y") == ("a", "a.x")


def test_resolve_path_prefix_root_segment_missing() -> None:
    # A root-level miss: nothing resolves, the missing prefix IS the first
    # segment itself.
    assert resolve_path_prefix({"a": 1}, "x.y.z") == ("", "x")


def test_resolve_path_prefix_intermediate_not_a_mapping() -> None:
    # "a.b" resolves (to a scalar) but cannot be descended into, so the
    # missing prefix is the next segment's prefix.
    assert resolve_path_prefix({"a": {"b": 5}}, "a.b.c") == ("a.b", "a.b.c")


def test_resolve_path_prefix_rejects_list_suffix() -> None:
    with pytest.raises(ValueError, match="list suffix"):
        resolve_path_prefix({"a": [1, 2]}, "a[*]")
    with pytest.raises(ValueError, match="list suffix"):
        resolve_path_prefix({"a": [1, 2]}, "a[]")


def test_resolve_path_prefix_yaml_model() -> None:
    model = _yload("a:\n  b: 1  # comment\n")
    assert resolve_path_prefix(model, "a.b") == ("a.b", None)
    assert resolve_path_prefix(model, "a.z") == ("a", "a.z")
    assert resolve_path_prefix(model, "q.r") == ("", "q")
    assert resolve_path_prefix(model, "a.b.c") == ("a.b", "a.b.c")


def test_resolve_path_prefix_jsonc_model() -> None:
    model = _jload('{\n  "a": {\n    "b": 1 // comment\n  }\n}')
    assert resolve_path_prefix(model, "a.b") == ("a.b", None)
    assert resolve_path_prefix(model, "a.z") == ("a", "a.z")
    assert resolve_path_prefix(model, "q.r") == ("", "q")
    assert resolve_path_prefix(model, "a.b.c") == ("a.b", "a.b.c")


def test_set_at_path_rejects_list_suffix() -> None:
    # I10: list-index pins are rejected at the set seam.
    with pytest.raises(ValueError, match="list suffix"):
        set_at_path({"a": [1]}, "a[*]", 9)


def test_set_at_path_missing_parent_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        set_at_path({"a": 1}, "x.y", 9)


def test_set_at_path_parent_not_mapping_raises_mismatch() -> None:
    with pytest.raises(MergeTypeMismatch):
        set_at_path({"a": 5}, "a.b", 9)


def test_theirs_deletes_container_ours_unchanged_yaml() -> None:
    # Regression: ours==base, theirs DELETED a nested-map key -> the key is
    # dropped (a take toward the deleting side), no KeyError mid-merge.
    base = _yload("a:\n  b: 1\nkeep: yes\n")
    ours = _yload("a:\n  b: 1\nkeep: yes\n")
    theirs = _yload("keep: yes\n")
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.merged_model == {"keep": "yes"}


def test_ours_deletes_container_theirs_unchanged_yaml() -> None:
    # Symmetric: theirs==base, ours DELETED the nested-map key -> stays deleted.
    base = _yload("a:\n  b: 1\nkeep: yes\n")
    ours = _yload("keep: yes\n")
    theirs = _yload("a:\n  b: 1\nkeep: yes\n")
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert result.merged_model == {"keep": "yes"}


# ---------------------------------------------------------------------------
# list_keys_at_path — sibling-key enumeration for did-you-mean diagnostics.
# ---------------------------------------------------------------------------


def test_list_keys_at_path_root_yaml() -> None:
    model = _yload("alpha: 1\nbeta: 2\n")
    assert list_keys_at_path(model, "") == ["alpha", "beta"]


def test_list_keys_at_path_nested_jsonc() -> None:
    model = _jload('{"editor": {"fontSize": 12, "tabSize": 4}}')
    assert list_keys_at_path(model, "editor") == ["fontSize", "tabSize"]


def test_list_keys_at_path_plain_dict() -> None:
    assert list_keys_at_path({"a": {"b": 1, "c": 2}}, "a") == ["b", "c"]


def test_list_keys_at_path_absent_or_scalar_returns_empty() -> None:
    model = _yload("alpha: 1\n")
    assert list_keys_at_path(model, "missing") == []
    assert list_keys_at_path(model, "alpha") == []


def test_list_keys_at_path_list_suffix_raises() -> None:
    model = _yload("alpha: [1]\n")
    with pytest.raises(ValueError, match="list suffix"):
        list_keys_at_path(model, "alpha.[*]")


# --------------------------------------------------------------------------
# Empty-path "" addresses the ROOT on every navigation seam (one meaning).
# --------------------------------------------------------------------------


def test_empty_path_is_root_on_all_seams_plain_dict() -> None:
    # "" means "the root node" identically on all four seams: the get seams
    # return the whole root mapping, resolve reports a fully-resolved root,
    # and list_keys enumerates the root keys.
    model = {"a": 1, "b": {"c": 2}}
    assert get_at_path(model, "") == {"a": 1, "b": {"c": 2}}
    assert get_node_at_path(model, "") == {"a": 1, "b": {"c": 2}}
    assert resolve_path_prefix(model, "") == ("", None)
    assert list_keys_at_path(model, "") == ["a", "b"]


def test_empty_path_is_root_not_empty_string_key_lookup() -> None:
    # The old ambiguity: "" was a lookup of the empty-string KEY on the get /
    # resolve seams but ROOT on list_keys. Now "" is ROOT everywhere — even
    # when an empty-string key is present, "" returns the whole root, NOT that
    # key's value.
    model = {"": {"a": 1}, "top": 5}
    # get seams: whole root, not {"a": 1}.
    assert get_at_path(model, "") == {"": {"a": 1}, "top": 5}
    assert get_node_at_path(model, "") == {"": {"a": 1}, "top": 5}
    # resolve: fully resolved to root, not an empty-key hit.
    assert resolve_path_prefix(model, "") == ("", None)
    # list_keys: root keys (unchanged behavior).
    assert list_keys_at_path(model, "") == ["", "top"]


def test_empty_path_is_root_when_no_empty_string_key_present() -> None:
    # Without an empty-string key the seams still agree on root; the get seams
    # no longer collapse to ABSENT (the old empty-key miss).
    model = {"top": 5}
    assert get_at_path(model, "") == {"top": 5}
    assert get_at_path(model, "") is not ABSENT
    assert get_node_at_path(model, "") == {"top": 5}
    assert get_node_at_path(model, "") is not ABSENT
    assert resolve_path_prefix(model, "") == ("", None)
    assert list_keys_at_path(model, "") == ["top"]


def test_empty_path_root_yaml_backend() -> None:
    model = _yload("alpha: 1\nbeta:\n  gamma: 2  # c\n")
    assert get_at_path(model, "") == {"alpha": 1, "beta": {"gamma": 2}}
    assert get_node_at_path(model, "") == {"alpha": 1, "beta": {"gamma": 2}}
    assert resolve_path_prefix(model, "") == ("", None)
    assert list_keys_at_path(model, "") == ["alpha", "beta"]


def test_empty_path_root_jsonc_backend() -> None:
    model = _jload('{\n  "a": 1, // c\n  "b": {"c": 2}\n}')
    assert get_at_path(model, "") == {"a": 1, "b": {"c": 2}}
    # get_node_at_path returns the still-WRAPPED root node; unwrap to compare.
    node = get_node_at_path(model, "")
    assert isinstance(node, JSONObject)
    assert _to_plain(node) == {"a": 1, "b": {"c": 2}}
    assert resolve_path_prefix(model, "") == ("", None)
    assert list_keys_at_path(model, "") == ["a", "b"]


def test_empty_path_root_node_is_deep_copy() -> None:
    # get_node_at_path("") deep-copies the root, so a later mutation of the
    # source cannot clobber the captured node (B-S1/B-S2).
    model = {"a": {"b": 1}}
    node = get_node_at_path(model, "")
    model["a"]["b"] = 999
    assert node == {"a": {"b": 1}}


# --------------------------------------------------------------------------
# Duplicate keys in a json5 object are rejected fail-closed (no lossy view).
# --------------------------------------------------------------------------


def test_to_plain_rejects_duplicate_json5_keys() -> None:
    # A json5 object may carry duplicate keys (legal in JSON5/JSONC). The old
    # _to_plain collapsed them last-wins ({"a":1,"a":2} -> {"a":2}), silently
    # dropping the first pair so the divergence test compared a lossy view.
    # Now the unwrap fails closed rather than mis-decide a merge.
    inner = _json5_inner(_jload('{"a": 1, "a": 2}'))
    with pytest.raises(DuplicateKeyInMergeModel, match="a"):
        _to_plain(inner)


def test_to_plain_rejects_nested_duplicate_json5_keys() -> None:
    # The refusal reaches nested objects too, not just the top level.
    inner = _json5_inner(_jload('{"outer": {"k": 1, "k": 2}}'))
    with pytest.raises(DuplicateKeyInMergeModel, match="k"):
        _to_plain(inner)


def test_to_plain_no_duplicate_keys_unwraps_normally() -> None:
    # The common no-duplicate case is unchanged: a faithful plain unwrap.
    inner = _json5_inner(_jload('{"a": 1, "b": {"c": 2}}'))
    assert _to_plain(inner) == {"a": 1, "b": {"c": 2}}


def test_merge_rejects_duplicate_json5_keys_in_divergence_test() -> None:
    # End-to-end: a duplicate-key object reaching the whole-subtree divergence
    # test (_check_no_shape_mismatch / _resolve_opaque -> _to_plain) fails
    # closed. The subtree is compared WHOLE only when the three sides are not
    # all mappings at that key: here base/theirs keep "a" a scalar while ours
    # turns it into the dup-key object, so the shape check unwraps ours' "a".
    base = _jload('{"a": 0}')
    ours = _jload('{"a": {"x": 1, "x": 2}}')
    theirs = _jload('{"a": 0}')
    with pytest.raises(DuplicateKeyInMergeModel, match="x"):
        merge_structural(base, ours, theirs)


@pytest.mark.parametrize("bare_root", [False, True])
def test_merge_rejects_duplicate_json5_keys_before_mapping_recursion(
    bare_root: bool,
) -> None:
    base = _jload('{"advance": 1, "outer": {"x": 1}}')
    ours = _jload('{"advance": 1, "outer": {"x": 1}}')
    theirs = _jload('{"advance": 2, "outer": {"x": 2, "x": 3}}')
    if bare_root:
        base, ours, theirs = map(_json5_inner, (base, ours, theirs))
    before = _jdump(ours)

    with pytest.raises(DuplicateKeyInMergeModel, match="x"):
        merge_structural(base, ours, theirs)

    assert _jdump(ours) == before


# --------------------------------------------------------------------------
# Injective dotted-key-path codec.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "segments",
    [
        ["a", "b", "c"],  # plain nested path
        ["a.b"],  # a single key carrying a literal dot
        ["a\\b"],  # a single key carrying a backslash
        [""],  # an empty segment
        ["a", "", "c"],  # an empty middle segment
        ["ключ", "café"],  # unicode keys
        ["a.b", "c.d"],  # two dotted keys
        ["weird\\.key", "x"],  # backslash AND dot in one segment
    ],
)
def test_codec_round_trips_via_append(segments: list[str]) -> None:
    """A path built segment-by-segment via ``append_key_segment`` splits back
    to the exact original segment list, for dots / backslashes / empties /
    unicode."""
    path: str | None = None
    for seg in segments:
        path = append_key_segment(path, seg)
    assert path is not None
    assert split_key_path(path) == segments


def test_split_matches_str_split_for_escape_free_path() -> None:
    """Byte-compat: for any path with no backslash the codec split equals the
    old ``path.split(".")`` — so existing persisted rows keep matching."""
    assert split_key_path("a.b.c") == ["a", "b", "c"]
    # Intentional str.split comparison: the whole point is byte-for-byte parity
    # with the pre-fix path.split(".") for any escape-free path.
    assert split_key_path("a.b.c") == "a.b.c".split(".")  # noqa: SIM905
    assert split_key_path("alpha") == ["alpha"]


def test_encode_identity_for_ordinary_key() -> None:
    """A key with neither ``.`` nor ``\\`` encodes to itself (identity), and
    ``append_key_segment`` reproduces the old bare join byte-for-byte."""
    assert encode_key_segment("plainKey") == "plainKey"
    assert append_key_segment(None, "plainKey") == "plainKey"
    assert append_key_segment("a.b", "c") == "a.b.c"


def test_empty_key_has_canonical_non_root_encoding() -> None:
    """The staged root identity and a genuine empty mapping key are disjoint."""
    assert append_key_segment(None, "") == r"\0"
    assert split_key_path(r"\0") == [""]
    assert split_key_path("") == [""]  # legacy path remains readable
    assert append_key_segment(None, r"\0") == r"\\0"
    assert split_key_path(r"\\0") == [r"\0"]


def test_flat_dotted_key_distinct_from_nested_path() -> None:
    """The flat key ``"a.b"`` encodes distinctly from the nested path
    ``a -> b``, and each round-trips to its own segment list."""
    flat = append_key_segment(None, "a.b")
    nested = append_key_segment(append_key_segment(None, "a"), "b")
    assert flat != nested
    assert flat == "a\\.b"
    assert nested == "a.b"
    assert split_key_path(flat) == ["a.b"]
    assert split_key_path(nested) == ["a", "b"]


def test_join_key_segments_reencodes_dotted_segments() -> None:
    """``join_key_segments`` re-escapes so a decoded dotted segment round-trips
    and equals a bare join only when no segment carries ``.``/``\\``."""
    assert join_key_segments(["a", "b", "c"]) == "a.b.c"
    assert split_key_path(join_key_segments(["a.b", "c"])) == ["a.b", "c"]


def test_merge_flat_dotted_key_and_nested_path_do_not_collide() -> None:
    """The MERGE use-site keeps a flat ``"a.b"`` key distinct from nested ``a -> b``.

    Guards ``_merge_key``'s ``append_key_segment`` threading: a doc carrying BOTH
    a literal flat ``"a.b"`` key AND a nested ``a: {b: …}`` that BOTH diverge must
    record TWO conflicts at DISTINCT paths. A use-site revert to a bare ``.``-join
    would label both conflicts ``"a.b"`` and collapse this assertion — so this
    covers the merge use-site the codec-unit tests never exercise.
    """
    base = {"a.b": 0, "a": {"b": 0}}
    ours = {"a.b": 1, "a": {"b": 1}}  # live changed both leaves
    theirs = {"a.b": 2, "a": {"b": 2}}  # upstream changed both differently

    result = merge_structural(base, ours, theirs)

    assert not result.clean
    assert {c.path for c in result.conflicts} == {"a\\.b", "a.b"}


@pytest.mark.parametrize(
    "name",
    [
        "settings.json",
        "config.yaml",
        "config.yml",
    ],
)
def test_is_structural_true_for_json_and_yaml(name: str) -> None:
    assert is_structural(Path("/some/dir") / name) is True


@pytest.mark.parametrize(
    "name",
    [
        "notes.md",
        "plain.txt",
        "README",
        "archive.yaml.bak",
    ],
)
def test_is_structural_false_for_non_structural(name: str) -> None:
    assert is_structural(Path("/some/dir") / name) is False


@pytest.mark.parametrize("backend", ["plain", "yaml", "jsonc"])
def test_competing_added_mappings_with_distinct_keys_remain_conflicts(
    backend: str,
) -> None:
    import json

    def model(value: dict[str, object]) -> object:
        text = json.dumps(value)
        return (
            _jload(text)
            if backend == "jsonc"
            else _yload(text)
            if backend == "yaml"
            else value
        )

    result = merge_structural(
        model({}), model({"new": {"left": 1}}), model({"new": {"right": 2}})
    )
    assert not result.clean
    assert result.conflicts == [
        PathConflict(path="new", base=ABSENT, ours={"left": 1}, theirs={"right": 2})
    ]


@pytest.mark.parametrize(
    ("base", "ours", "theirs"),
    [
        ([1], [2], [3]),
        ([1], [True], [1.0]),
        ([{"value": None}], [{"value": ""}], [{"value": {}}]),
    ],
)
def test_jsonc_opaque_array_conflicts_keep_original_typed_values(
    base: list[object], ours: list[object], theirs: list[object]
) -> None:
    import json

    result = merge_structural(
        *(_jload(json.dumps({"items": value})) for value in (base, ours, theirs))
    )
    assert not result.clean
    assert result.conflicts == [
        PathConflict(path="items", base=base, ours=ours, theirs=theirs)
    ]


def test_jsonc_identifier_keys_merge_with_exact_names_and_values() -> None:
    base = _jload("{bare: 1, quoted: 0}")
    ours = _jload("{bare: 1, quoted: 2}")
    theirs = _jload("{bare: 3, quoted: 0}")
    result = merge_structural(base, ours, theirs)
    assert result.clean
    assert json5_loads(_jdump(result.merged_model)) == {"bare": 3, "quoted": 2}


def test_jsonc_identifier_keys_in_opaque_values_keep_exact_conflict_names() -> None:
    result = merge_structural(
        _jload("{items: [{bare: 1}]}"),
        _jload("{items: [{bare: 2}]}"),
        _jload("{items: [{bare: 3}]}"),
    )
    assert not result.clean
    assert result.conflicts == [
        PathConflict(
            path="items", base=[{"bare": 1}], ours=[{"bare": 2}], theirs=[{"bare": 3}]
        )
    ]


def test_jsonc_added_and_deleted_keys_preserve_both_side_membership() -> None:
    result = merge_structural(
        _jload("{old: 1, removed: 2}"),
        _jload("{old: 1, removed: 2, local: true}"),
        _jload("{old: 3, added: {nested: false}}"),
    )
    assert result.clean
    assert json5_loads(_jdump(result.merged_model)) == {
        "old": 3,
        "local": True,
        "added": {"nested": False},
    }


@pytest.mark.parametrize("backend", ["plain", "yaml", "jsonc"])
@pytest.mark.parametrize("side", ["ours", "theirs"])
@pytest.mark.parametrize("replacement", [[1], {"nested": None}])
def test_one_sided_container_shape_replacement_is_a_clean_take(
    backend: str, side: str, replacement: object
) -> None:
    import json

    def model(value: dict[str, object]) -> object:
        text = json.dumps(value)
        return (
            _jload(text)
            if backend == "jsonc"
            else _yload(text)
            if backend == "yaml"
            else value
        )

    base: dict[str, object] = {"key": 1}
    changed: dict[str, object] = {"key": replacement}
    result = merge_structural(
        model(base),
        model(changed if side == "ours" else base),
        model(changed if side == "theirs" else base),
    )
    assert result.clean

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        assert len(pairs) == len({key for key, _value in pairs})
        return dict(pairs)

    merged = (
        json.loads(_jdump(result.merged_model), object_pairs_hook=unique_pairs)
        if backend == "jsonc"
        else result.merged_model
    )
    assert merged == changed


@pytest.mark.parametrize("backend", ["plain", "yaml", "jsonc"])
@pytest.mark.parametrize("original", [[1], {"nested": None}])
def test_distinct_scalar_replacements_of_a_container_remain_conflicts(
    backend: str, original: object
) -> None:
    import json

    def model(value: dict[str, object]) -> object:
        text = json.dumps(value)
        return (
            _jload(text)
            if backend == "jsonc"
            else _yload(text)
            if backend == "yaml"
            else value
        )

    result = merge_structural(
        model({"key": original}), model({"key": 2}), model({"key": 3})
    )
    assert not result.clean
    assert result.conflicts == [
        PathConflict(path="key", base=original, ours=2, theirs=3)
    ]


def test_yaml_explicit_string_tags_remain_distinct_scalar_conflicts() -> None:
    result = merge_structural(
        _yload("key: !!str 1\n"), _yload("key: !!str 2\n"), _yload("key: !!str 3\n")
    )
    assert not result.clean
    assert result.conflicts == [
        PathConflict(path="key", base="1", ours="2", theirs="3")
    ]


@pytest.mark.parametrize(
    ("token", "expected", "primitive"),
    [
        ("-1", -1, int),
        ("+1", 1, int),
        ("-1.5", -1.5, float),
        ("-0.0", -0.0, float),
        ("-Infinity", float("-inf"), float),
        ("+Infinity", float("inf"), float),
    ],
)
def test_jsonc_signed_scalar_unwraps_to_exact_primitive(
    token: str, expected: object, primitive: type
) -> None:
    import math

    from json5.model import JSONText

    from setforge.structural_merge import _to_plain

    model = _jload("{number: " + token + "}")
    assert isinstance(model, JSONText)
    parsed = _to_plain(model.value)
    assert isinstance(parsed, dict)
    value = parsed["number"]
    assert type(value) is primitive
    assert value == expected
    if token == "-0.0":
        assert isinstance(value, float)
        assert math.copysign(1.0, value) == -1.0


@pytest.mark.parametrize("backend", ["yaml", "jsonc"])
def test_equal_scalar_values_keep_live_comment_provenance(backend: str) -> None:
    load: Callable[[str], object]
    dump: Callable[[object], str]
    if backend == "yaml":
        load, dump = _yload, _ydump
        base, ours, theirs = "key: 1 # base\n", "key: 1 # live\n", "key: 1 # upstream\n"
    else:
        load, dump = _jload, _jdump
        base, ours, theirs = (
            '{"key": 1 // base\n}',
            '{"key": 1 // live\n}',
            '{"key": 1 // upstream\n}',
        )
    expected = dump(load(ours))
    result = merge_structural(load(base), load(ours), load(theirs))
    assert result.clean
    assert dump(result.merged_model) == expected


@pytest.mark.parametrize("backend", ["plain", "yaml", "jsonc"])
@pytest.mark.parametrize("added", [[1], {"nested": None}])
def test_identical_added_containers_are_clean_and_retained(
    backend: str, added: object
) -> None:
    import json

    def model(value: dict[str, object]) -> object:
        text = json.dumps(value)
        return (
            _jload(text)
            if backend == "jsonc"
            else _yload(text)
            if backend == "yaml"
            else value
        )

    expected = {"new": added}
    result = merge_structural(model({}), model(expected), model(expected))
    assert result.clean
    assert not result.conflicts
    merged = (
        json5_loads(_jdump(result.merged_model))
        if backend == "jsonc"
        else result.merged_model
    )
    assert merged == expected


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("1: a\n", "non-string mapping key 1 (int) cannot be addressed by a key path"),
        (
            "~: a\n",
            "non-string mapping key None (NoneType) cannot be addressed by a key path",
        ),
        (
            "m:\n  true: a\n",
            "non-string mapping key True (bool) cannot be addressed by a key path",
        ),
    ],
)
def test_non_string_yaml_key_is_a_type_mismatch(document: str, message: str) -> None:
    base, ours, theirs = (_yload(document) for _ in range(3))

    with pytest.raises(MergeTypeMismatch) as excinfo:
        merge_structural(base, ours, theirs)

    assert str(excinfo.value) == message


# --------------------------------------------------------------------------
# json-five layout: spliced members follow the live file's separators.
# --------------------------------------------------------------------------

_JSON_LAYOUT_CASES = [
    pytest.param(
        '{\n  "a": 1,\n  "c": 3 // last\n}\n',
        '{\n  "a": 1,\n  "c": 3, // last\n  "hostonly": true\n}\n',
        '{\n  "a": 1,\n  "c": 4 // last\n}\n',
        '{\n  "a": 1,\n  "c": 4, // last\n  "hostonly": true\n}\n',
        id="take-theirs-last-into-ours-non-last",
    ),
    pytest.param(
        '{\n  "a": 1,\n  "c": 3, // c\n  "z": 0\n}\n',
        '{\n  "a": 1,\n  "c": 3 // c\n}\n',
        '{\n  "a": 1,\n  "c": 4, // c\n  "z": 0\n}\n',
        '{\n  "a": 1,\n  "c": 4 // c\n}\n',
        id="take-theirs-non-last-into-ours-last",
    ),
    pytest.param(
        '{\n  "a": 1,\n  "c": 3,\n}\n',
        '{\n  "a": 2,\n  "c": 3, // mine\n}\n',
        '{\n  "a": 1,\n  "c": 4 // theirs\n}\n',
        '{\n  "a": 2,\n  "c": 4, // mine\n}\n',
        id="take-theirs-across-trailing-comma-styles",
    ),
    pytest.param(
        '{\n  "a": 1\n}\n',
        '{\n  "a": 1,\n  "host": true\n}\n',
        '{\n  "a": 1,\n  "d": 4\n}\n',
        '{\n  "a": 1,\n  "host": true,\n  "d": 4\n}\n',
        id="add-after-last",
    ),
    pytest.param(
        '{\n  "a": 1\n}\n',
        '{\n  "a": 1,\n  "host": true // mine\n}\n',
        '{\n  "a": 1,\n  // why d\n  "d": 4 // four\n}\n',
        '{\n  "a": 1,\n  "host": true, // mine\n  // why d\n  "d": 4 // four\n}\n',
        id="add-carries-its-own-comments",
    ),
    pytest.param(
        '{\n  "a": 1\n}\n',
        '{\n  "a": 1,\n  "host": true\n}\n',
        '{\n  "d": 4, // four\n  "a": 1\n}\n',
        '{\n  "a": 1,\n  "host": true,\n  "d": 4 // four\n}\n',
        id="add-of-a-non-last-member",
    ),
    pytest.param(
        '{\n  "a": 1,\n}\n',
        '{\n  "a": 1,\n  "host": true, // mine\n}\n',
        '{\n  "a": 1,\n  "d": 4, // four\n}\n',
        '{\n  "a": 1,\n  "host": true, // mine\n  "d": 4, // four\n}\n',
        id="add-with-trailing-comma",
    ),
    pytest.param(
        '{\n  "a": 1\n}\n',
        '{\n  "a": 2\n}\n',
        '{\n  "a": 1,\n  "d": 4\n}\n',
        '{\n  "a": 2,\n  "d": 4\n}\n',
        id="add-to-single-member",
    ),
    pytest.param(
        '{"a":1}',
        '{"a":1,"h":true}',
        '{"a":1,"d":4}',
        '{"a":1,"h":true,"d":4}',
        id="add-compact",
    ),
    pytest.param(
        '{ "a": 1 }',
        '{ "a": 1, "h": true }',
        '{ "a": 1, "d": 4 }',
        '{ "a": 1, "h": true, "d": 4 }',
        id="add-spaced-one-line",
    ),
    pytest.param(
        '{\r\n\t"a": 1\r\n}\r\n',
        '{\r\n\t"a": 1,\r\n\t"h": true\r\n}\r\n',
        '{\r\n\t"a": 1,\r\n\t"d": 4\r\n}\r\n',
        '{\r\n\t"a": 1,\r\n\t"h": true,\r\n\t"d": 4\r\n}\r\n',
        id="add-tabs-crlf",
    ),
    pytest.param(
        '{\r\n  "a": 1,\r\n  "c": 3 // last\r\n}\r\n',
        '{\r\n  "a": 1,\r\n  "c": 3, // last\r\n  "h": true\r\n}\r\n',
        '{\r\n  "a": 1,\r\n  "c": 3, // last\r\n  "d": 4\r\n}\r\n',
        '{\r\n  "a": 1,\r\n  "c": 3, // last\r\n  "h": true,\r\n  "d": 4\r\n}\r\n',
        id="add-crlf-after-a-commented-member",
    ),
    pytest.param(
        '{\r\n  "a": 1 // one\r\n}\r\n',
        '{\r\n  "a": 2 // one\r\n}\r\n',
        '{\r\n  "a": 1, // one\r\n  // why d\r\n  "d": 4 // four\r\n}\r\n',
        '{\r\n  "a": 2, // one\r\n  // why d\r\n  "d": 4 // four\r\n}\r\n',
        id="add-crlf-commented-member-after-a-comment",
    ),
    pytest.param(
        '{\r\n  "a": 1, // one\r\n  "b": 2, // two\r\n  "c": 3 // three\r\n}\r\n',
        '{\r\n  "a": 5, // one\r\n  "b": 2, // two\r\n  "c": 3 // three\r\n}\r\n',
        '{\r\n  "a": 1, // one\r\n  "b": 2 // two\r\n}\r\n',
        '{\r\n  "a": 5, // one\r\n  "b": 2 // two\r\n}\r\n',
        id="delete-last-crlf-with-comments",
    ),
    pytest.param(
        '{\r\n  "a": 1, // one\r\n}\r\n',
        '{\r\n  "a": 2, // one\r\n}\r\n',
        '{\r\n  "a": 1, // one\r\n  "d": 4, // four\r\n}\r\n',
        '{\r\n  "a": 2, // one\r\n  "d": 4, // four\r\n}\r\n',
        id="add-crlf-with-commented-trailing-comma",
    ),
    pytest.param(
        '{\r\n  // head\r\n  "a": 1\r\n}\r\n',
        '{\r\n  // head\r\n  "a": 2\r\n}\r\n',
        '{\r\n  // head\r\n  "a": 1,\r\n  "d": 4\r\n}\r\n',
        '{\r\n  // head\r\n  "a": 2,\r\n  "d": 4\r\n}\r\n',
        id="add-crlf-below-a-header-comment",
    ),
    pytest.param(
        '{\r\n  "a": 1,\r\n  // about b\r\n  "b": 2,\r\n  "c": 3\r\n}\r\n',
        '{\r\n  "a": 1,\r\n  // about b\r\n  "b": 2,\r\n  "c": 9\r\n}\r\n',
        '{\r\n  // about b\r\n  "b": 2,\r\n  "c": 3\r\n}\r\n',
        '{\r\n  // about b\r\n  "b": 2,\r\n  "c": 9\r\n}\r\n',
        id="delete-first-crlf-keeps-successor-lead-comment",
    ),
    pytest.param(
        '{ "a": 1 // one\n}\n',
        '{ "a": 2 // one\n}\n',
        '{ "a": 1, // one\n  "d": 4\n}\n',
        '{ "a": 2, // one\n "d": 4\n}\n',
        id="add-after-line-comment-starts-a-new-line",
    ),
    pytest.param(
        '{ "a": 1 }',
        '{ "a": 2 }',
        '{ "a": 1,\n  "d": 4 // four\n}',
        '{ "a": 2, "d": 4 }',
        id="add-drops-a-line-comment-that-would-swallow-the-brace",
    ),
    pytest.param(
        '{\n  "a": 1\n}\n',
        '{\n  "a": 1,\n  "host": true\n}\n',
        '{\n  // header\n  "d": 4,\n  "a": 1\n}\n',
        '{\n  "a": 1,\n  "host": true,\n  "d": 4\n}\n',
        id="add-of-the-first-member-leaves-the-header-comment",
    ),
    pytest.param(
        '{\n  "o": {},\n  "k": 1\n}\n',
        '{\n  "o": {},\n  "k": 2\n}\n',
        '{\n  "o": {\n    "x": 1,\n    "y": 2\n  },\n  "k": 1\n}\n',
        '{\n  "o": {\n    "x": 1,\n    "y": 2\n  },\n  "k": 2\n}\n',
        id="add-into-empty-object",
    ),
    pytest.param(
        '{\n  "a": 1, // one\n  "b": 2, // two\n  "c": 3 // three\n}\n',
        '{\n  "a": 5, // one\n  "b": 2, // two\n  "c": 3 // three\n}\n',
        '{\n  "a": 1, // one\n  "c": 3 // three\n}\n',
        '{\n  "a": 5, // one\n  "c": 3 // three\n}\n',
        id="delete-middle",
    ),
    pytest.param(
        '{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n',
        '{\n  "a": 5,\n  // host note\n  "b": 2,\n  "c": 3\n}\n',
        '{\n  "a": 1,\n  "c": 3\n}\n',
        '{\n  "a": 5,\n  // host note\n  "c": 3\n}\n',
        id="delete-middle-keeps-a-host-comment-above-it",
    ),
    pytest.param(
        '{\n  "a": 1, // one\n  // about b\n  // more\n  "b": 2,\n  "c": 3\n}\n',
        '{\n  "a": 5, // one\n  // about b\n  // more\n  "b": 2,\n  "c": 3\n}\n',
        '{\n  "a": 1, // one\n  "c": 3\n}\n',
        '{\n  "a": 5, // one\n  "c": 3\n}\n',
        id="delete-middle-drops-the-comments-upstream-removed-with-it",
    ),
    pytest.param(
        '{\n  "a": 1, // one\n  // about b\n  // more\n  "b": 2,\n  "c": 3\n}\n',
        '{\n  "a": 5, // one\n  // about b\n  // more\n  "b": 2,\n  "c": 3\n}\n',
        '{\n  "a": 1, // one\n  // more\n  "c": 3\n}\n',
        '{\n  "a": 5, // one\n  // more\n  "c": 3\n}\n',
        id="delete-middle-keeps-the-comment-upstream-kept",
    ),
    pytest.param(
        '{\n  "a": 1,\n  // note\n  "b": 2\n}\n',
        '{\n  "a": 5,\n  // note\n  "b": 2\n}\n',
        '{\n  "a": 1\n  // note\n}\n',
        '{\n  "a": 5\n  // note\n}\n',
        id="delete-last-keeps-the-comment-upstream-kept",
    ),
    pytest.param(
        '{\n  "a": 1,\n  // note\n  "b": 2,\n}\n',
        '{\n  "a": 5,\n  // note\n  // mine\n  "b": 2,\n}\n',
        '{\n  "a": 1,\n}\n',
        '{\n  "a": 5,\n  // mine\n}\n',
        id="delete-last-with-trailing-comma-keeps-only-the-host-comment",
    ),
    pytest.param(
        '{\n  "a": 1,\n  // upstream note\n  "b": 2\n}\n',
        '{\n  "a": 1\n  // upstream note\n}\n',
        '{\n  "a": 1,\n  // upstream note\n  // why t\n  "t": 5,\n  "b": 2\n}\n',
        '{\n  "a": 1,\n  // why t\n  "t": 5\n  // upstream note\n}\n',
        id="add-does-not-repeat-a-comment-ours-already-has",
    ),
    pytest.param(
        '{\n  "a": 1,\n  "b": 2 // base b\n}\n',
        '{\n  "a": 9,\n  "b": 2 // base b\n  // host note\n}\n',
        '{\n  "a": 1,\n  "b": 16 // their b\n}\n',
        '{\n  "a": 9,\n  "b": 16 // their b\n  // host note\n}\n',
        id="take-theirs-last-keeps-ours-lines-below-the-value",
    ),
    pytest.param(
        '{\n  "a": 1,\n  // note\n  "b": 2\n}\n',
        '{\n  "h": 0,\n  "a": 1,\n  // note\n  "b": 2\n}\n',
        '{\n  "a": 5 // five\n  // note\n}\n',
        '{\n  "h": 0,\n  "a": 5\n  // note\n}\n',
        id="delete-last-after-taking-the-value-that-held-the-kept-comment",
    ),
    pytest.param(
        '{\n  "a": 1\n}\n',
        "{\n  // host header\n}\n",
        '{\n    "t": 2\n}\n',
        '{\n  // host header\n    "t": 2\n}\n',
        id="add-into-an-emptied-object-keeps-ours-comment",
    ),
    pytest.param(
        '{\n  "a": 1,\n  "z": 0\n}\n',
        '{\n  "a": 2,\n  "z": 0\n}\n',
        '{\n  "a": 1, // one\n  "d": 4, // four\n  "z": 0\n}\n',
        '{\n  "a": 2,\n  "z": 0,\n  "d": 4 // four\n}\n',
        id="add-of-a-middle-member-takes-the-comment-after-its-own-comma",
    ),
    pytest.param(
        '{\n  "a": 1, "b": 2,\n    "c": 3\n}\n',
        '{\n  "a": 2, "b": 2,\n    "c": 3\n}\n',
        '{\n  "a": 1, "b": 2,\n    "c": 3,\n    "d": 4\n}\n',
        '{\n  "a": 2, "b": 2,\n    "c": 3,\n    "d": 4\n}\n',
        id="add-follows-the-last-member-not-the-second",
    ),
    pytest.param(
        '{ "a": 1,\n  "b": 2\n}\n',
        '{ "a": 2,\n  "b": 2\n}\n',
        '{ "a": 1,\n  "b": 2,\n  "d": 4\n}\n',
        '{ "a": 2,\n  "b": 2,\n  "d": 4\n}\n',
        id="add-to-two-members-follows-the-second",
    ),
    pytest.param(
        '{\n  "x": 1,\n}\n',
        '{\n  "x": 1,\n}\n',
        '{\n  "y": 2,\n  "z": 3\n}\n',
        '{\n  "y": 2,\n  "z": 3\n}\n',
        id="add-twice-into-an-object-emptied-of-its-trailing-comma",
    ),
    pytest.param(
        '{"a":1,"b":2}',
        '{"a":1,"b":3}',
        '{"b":2}',
        '{"b":3}',
        id="delete-first-compact",
    ),
    pytest.param(
        '{\n  "a": 1,\n  /* c */"b": 2\n}\n',
        '{\n  "a": 1,\n  /* c */"b": 3\n}\n',
        '{\n  /* c */"b": 2\n}\n',
        '{\n  /* c */"b": 3\n}\n',
        id="delete-first-keeps-a-comment-glued-to-the-successor",
    ),
    pytest.param(
        '{\n  "a": 1,\n  "b": 2\n}\n',
        '{\n  "a": 1,\n  "b": 3\n}\n',
        '{\n  "a": 5 /* five */,\n  "b": 2\n}\n',
        '{\n  "a": 5 /* five */,\n  "b": 3\n}\n',
        id="take-theirs-non-last-keeps-theirs-comment-before-the-comma",
    ),
    pytest.param(
        '{ "a": 1, "b": 2 }',
        '{ "a": 9, "b": 2 }',
        '{ "a": 1, "b": 3 }',
        '{ "a": 9, "b": 3 }',
        id="take-theirs-last-on-one-line",
    ),
    pytest.param(
        '{\n  "a": 1, // one\n  "b": 2, // two\n  "c": 3 // three\n}\n',
        '{\n  "a": 1, // one\n  "b": 2, // two\n  "c": 9 // three\n}\n',
        '{\n  "b": 2, // two\n  "c": 3 // three\n}\n',
        '{\n  "b": 2, // two\n  "c": 9 // three\n}\n',
        id="delete-first",
    ),
    pytest.param(
        '{\n  "a": 1,\n  // about b\n  "b": 2,\n  "c": 3\n}\n',
        '{\n  "a": 1,\n  // about b\n  "b": 2,\n  "c": 9\n}\n',
        '{\n  // about b\n  "b": 2,\n  "c": 3\n}\n',
        '{\n  // about b\n  "b": 2,\n  "c": 9\n}\n',
        id="delete-first-keeps-successor-lead-comment",
    ),
    pytest.param(
        '{\n  "a": 1, // one\n  "b": 2, // two\n  "c": 3 // three\n}\n',
        '{\n  "a": 5, // one\n  "b": 2, // two\n  "c": 3 // three\n}\n',
        '{\n  "a": 1, // one\n  "b": 2 // two\n}\n',
        '{\n  "a": 5, // one\n  "b": 2 // two\n}\n',
        id="delete-last",
    ),
    pytest.param(
        '{\n  "a": 1, // one\n  "b": 2, // two\n}\n',
        '{\n  "a": 5, // one\n  "b": 2, // two\n}\n',
        '{\n  "a": 1, // one\n}\n',
        '{\n  "a": 5, // one\n}\n',
        id="delete-last-with-trailing-comma",
    ),
    pytest.param(
        '{ "a": 1, "b": 2, "c": 3 }',
        '{ "a": 5, "b": 2, "c": 3 }',
        '{ "a": 1, "b": 2 }',
        '{ "a": 5, "b": 2 }',
        id="delete-last-spaced-one-line",
    ),
    pytest.param(
        '{ "a": 1, "b": 2, "c": 3 }',
        '{ "a": 1, "b": 2, "c": 9 }',
        '{ "a": 1, "c": 3 }',
        '{ "a": 1, "c": 9 }',
        id="delete-middle-spaced-one-line",
    ),
    pytest.param(
        '{\n  "o": {\n    // about x\n    "x": 1\n  },\n  "k": 1\n}\n',
        '{\n  "o": {\n    // about x\n    "x": 1\n  },\n  "k": 2\n}\n',
        '{\n  "o": {\n  },\n  "k": 1\n}\n',
        '{\n  "o": {\n    // about x\n  },\n  "k": 2\n}\n',
        id="delete-only-member",
    ),
    pytest.param(
        '{\n  "o": { "x": 1},\n  "k": 1\n}\n',
        '{\n  "o": { "x": 1},\n  "k": 2\n}\n',
        '{\n  "o": {},\n  "k": 1\n}\n',
        '{\n  "o": { },\n  "k": 2\n}\n',
        id="delete-only-member-keeps-leading-space",
    ),
    pytest.param(
        '{\n  "o": {"x": 1,},\n  "k": 1\n}\n',
        '{\n  "o": {"x": 1,},\n  "k": 2\n}\n',
        '{\n  "o": {},\n  "k": 1\n}\n',
        '{\n  "o": {},\n  "k": 2\n}\n',
        id="delete-only-member-drops-trailing-comma",
    ),
]


@pytest.mark.parametrize(("base", "ours", "theirs", "expected"), _JSON_LAYOUT_CASES)
def test_jsonc_spliced_members_follow_the_live_layout(
    base: str, ours: str, theirs: str, expected: str
) -> None:
    result = merge_structural(_jload(base), _jload(ours), _jload(theirs))

    assert result.clean
    assert _jdump(result.merged_model) == expected


def test_shape_mismatch_names_the_key_path() -> None:
    base, ours, theirs = _yload("a: 1\n"), _yload("a:\n  x: 1\n"), _yload("a: 2\n")

    with pytest.raises(MergeTypeMismatch) as excinfo:
        merge_structural(base, ours, theirs)

    assert str(excinfo.value) == (
        "type mismatch at 'a': ours is mapping, theirs is scalar"
    )


def test_ruamel_backend_add_brings_the_comment_of_the_side_it_adds_from() -> None:
    base = _yload("k: 1  # base c\n")
    ours = _yload("a: 1\n")
    theirs = _yload("k: 1  # their c\na: 1\n")

    _RuamelBackend(base, ours, theirs).add("base", "k")

    assert _ydump(ours) == "a: 1\nk: 1  # base c\n"


def _json5_backend(base: str, ours: str, theirs: str) -> _Json5Backend:
    models = [_json5_inner(_jload(text)) for text in (base, ours, theirs)]
    return _Json5Backend(*models)


def test_json5_backend_add_works_before_any_lookup_in_ours() -> None:
    backend = _json5_backend('{"a": 1}', '{"a": 1}', '{"a": 1, "d": 4}')

    backend.add("theirs", "d")

    assert backend.keys() == ["a", "d"]


def test_json5_backend_finds_a_member_it_added_after_an_earlier_lookup() -> None:
    backend = _json5_backend('{"a": 1}', '{"a": 1}', '{"a": 1, "d": 4}')
    assert backend.has("ours", "d") is False

    backend.add("theirs", "d")

    assert backend.has("ours", "d") is True
    backend.delete("d")
    assert backend.has("ours", "d") is False
    assert backend.keys() == ["a"]


def test_json5_backend_looks_every_key_up_in_a_table_built_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import structural_merge

    size = 60
    text = "{" + ", ".join(f'"k{n}": {n}' for n in range(size)) + "}"
    theirs = text.replace('"k7": 7', '"k7": 70')
    read: list[object] = []
    real = structural_merge._json5_key_text

    def counting(key_node: object) -> str:
        read.append(key_node)
        return real(key_node)

    monkeypatch.setattr(structural_merge, "_json5_key_text", counting)

    result = merge_structural(_jload(text), _jload(text), _jload(theirs))

    assert result.clean
    assert _jdump(result.merged_model) == theirs
    assert len(read) <= 12 * size
