"""Tests for the base-aware structural 3-way merge engine.

Covers the shape decisions (recurse vs opaque-take vs conflict vs
type-mismatch), type-aware wrapper-free equality, comment + key-order
provenance (golden-file dumps for BOTH ruamel YAML and json-five JSONC),
delete-vs-edit conflict detection, and byte-stable idempotency.
"""

import io

import pytest
from json5.dumper import ModelDumper
from json5.dumper import dumps as json5_dumps
from json5.loader import ModelLoader
from json5.loader import loads as json5_loads
from ruamel.yaml import YAML

from setforge.errors import MergeTypeMismatch
from setforge.scalar_merge import ABSENT
from setforge.structural_merge import (
    PathConflict,
    StructuralMergeResult,
    merge_structural,
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
