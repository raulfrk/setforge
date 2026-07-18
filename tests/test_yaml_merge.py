"""Tests for yaml_merge.overlay."""

import pytest

from setforge.errors import MergeTypeMismatch
from setforge.yaml_merge import (
    NodeShape,
    PathTokenKind,
    _parse_path,
    _shape,
    overlay,
)


def test_dotted_path_overlay() -> None:
    src = {"a": {"b": {"c": 1, "d": 99}}}
    live = {"a": {"b": {"c": 2}}}
    merged = overlay(src, live, ["a.b.c"])
    assert merged == {"a": {"b": {"c": 2, "d": 99}}}


def test_path_absent_in_live_keeps_src() -> None:
    src = {"a": {"b": 1}}
    live: dict[str, dict[str, int]] = {"a": {}}
    merged = overlay(src, live, ["a.b"])
    assert merged == {"a": {"b": 1}}


def test_path_absent_in_src_adds_live_key() -> None:
    src: dict[str, dict[str, int]] = {"a": {}}
    live = {"a": {"b": 99}}
    merged = overlay(src, live, ["a.b"])
    assert merged == {"a": {"b": 99}}


def test_list_each_replaces_per_index() -> None:
    src = {"items": [{"x": 1}, {"x": 2}, {"x": 3}]}
    live = {"items": [{"x": 10}, {"x": 20}]}
    merged = overlay(src, live, ["items[*]"])
    assert merged == {"items": [{"x": 10}, {"x": 20}, {"x": 3}]}


def test_list_each_appends_when_live_longer() -> None:
    src = {"items": [{"x": 1}]}
    live = {"items": [{"x": 10}, {"x": 20}]}
    merged = overlay(src, live, ["items[*]"])
    assert merged == {"items": [{"x": 10}, {"x": 20}]}


def test_list_whole_replaces_entire_list() -> None:
    src = {"items": [1, 2, 3, 4, 5]}
    live = {"items": [9, 8]}
    merged = overlay(src, live, ["items[]"])
    assert merged == {"items": [9, 8]}


def test_leaf_type_mismatch_raises() -> None:
    src = {"a": "scalar"}
    live = {"a": [1, 2]}
    with pytest.raises(MergeTypeMismatch, match="a"):
        overlay(src, live, ["a"])


def test_dict_vs_scalar_mismatch_raises() -> None:
    src = {"a": {"nested": "value"}}
    live = {"a": "scalar"}
    with pytest.raises(MergeTypeMismatch):
        overlay(src, live, ["a"])


def test_overlay_does_not_mutate_inputs() -> None:
    src = {"a": {"b": 1}}
    live = {"a": {"b": 2}}
    merged = overlay(src, live, ["a.b"])
    assert src == {"a": {"b": 1}}
    assert live == {"a": {"b": 2}}
    assert merged is not src


def test_invalid_path_raises_value_error() -> None:
    with pytest.raises(ValueError, match="invalid path"):
        overlay({}, {}, [".bad"])


def test_list_suffix_only_at_end() -> None:
    with pytest.raises(ValueError, match="only allowed at end"):
        overlay({}, {}, ["a[*].b"])


def test_multiple_paths_compose() -> None:
    src = {"a": 1, "b": {"c": 2, "d": 3}, "e": [1, 2]}
    live = {"a": 10, "b": {"c": 20}, "e": [99, 88, 77]}
    merged = overlay(src, live, ["a", "b.c", "e[]"])
    assert merged == {"a": 10, "b": {"c": 20, "d": 3}, "e": [99, 88, 77]}


# ---------------------------------------------------------------------------
# Deep-merge mode
# ---------------------------------------------------------------------------


def test_overlay_deep_merge_unions_keys() -> None:
    src = {"a": {"b": 1, "c": 2}}
    live = {"a": {"b": 99, "c": 2, "d": "new"}}
    merged = overlay(src, live, [], deep_key_paths=["a"])
    assert merged == {"a": {"b": 99, "c": 2, "d": "new"}}


def test_overlay_deep_merge_keeps_tracked_only_subkeys() -> None:
    """PRIMARY VALUE GATE — tracked-only sub-keys must survive deep-merge."""
    src = {"a": {"b": 1, "e": "tracked_only"}}
    live = {"a": {"b": 99}}
    merged = overlay(src, live, [], deep_key_paths=["a"])
    assert merged == {"a": {"b": 99, "e": "tracked_only"}}


def test_overlay_deep_merge_recurses_nested_dicts() -> None:
    src = {"a": {"b": {"c": {"x": 1, "y": 2}}}}
    live = {"a": {"b": {"c": {"x": 99}}}}
    merged = overlay(src, live, [], deep_key_paths=["a"])
    assert merged == {"a": {"b": {"c": {"x": 99, "y": 2}}}}


def test_overlay_deep_merge_raises_on_terminal_shape_mismatch() -> None:
    src = {"a": 5}
    live = {"a": {"b": 1}}
    with pytest.raises(MergeTypeMismatch) as exc_info:
        overlay(src, live, [], deep_key_paths=["a"])
    msg = str(exc_info.value)
    assert "a" in msg
    assert "scalar" in msg
    assert "dict" in msg


def test_overlay_deep_merge_raises_on_nested_shape_mismatch() -> None:
    src = {"a": {"b": 5}}
    live = {"a": {"b": {"c": 1}}}
    with pytest.raises(MergeTypeMismatch, match=r"a\.b"):
        overlay(src, live, [], deep_key_paths=["a"])


def test_overlay_deep_merge_whole_list_replace_at_nested_arrays() -> None:
    src = {"a": {"xs": [1, 2, 3]}}
    live = {"a": {"xs": [9]}}
    merged = overlay(src, live, [], deep_key_paths=["a"])
    assert merged == {"a": {"xs": [9]}}


def test_overlay_deep_merge_path_absent_in_src_adds_subtree() -> None:
    src: dict = {}
    live = {"a": {"b": 1}}
    merged = overlay(src, live, [], deep_key_paths=["a"])
    assert merged == {"a": {"b": 1}}
    # Deep-copied — mutating live must not bleed into result.
    assert merged["a"] is not live["a"]


def test_overlay_shallow_path_unchanged_when_deep_list_used() -> None:
    """Regression gate: positional/keyword-empty calls must keep
    today's shallow-only semantics. Locks the back-compat contract for
    every existing caller. xfail until Phase 3 lands the kwarg."""
    src = {"a": {"b": 1, "c": 2}}
    live = {"a": {"b": 99, "c": 2}}
    merged_kwargless = overlay(src, live, ["a.b"])
    merged_with_empty = overlay(src, live, ["a.b"], deep_key_paths=[])
    assert merged_kwargless == merged_with_empty
    assert merged_kwargless == {"a": {"b": 99, "c": 2}}


# ---------------------------------------------------------------------------
# StrEnum dispatch + byte-identical error-message guards
# ---------------------------------------------------------------------------


def test_node_shape_values_are_the_literal_strings() -> None:
    """The enum ``.value`` strings must equal the historical literals verbatim
    so interpolation into error messages stays byte-identical."""
    assert (NodeShape.DICT, NodeShape.LIST, NodeShape.SCALAR) == (
        "dict",
        "list",
        "scalar",
    )
    # f-string / str() render to the bare value, not "NodeShape.DICT".
    assert f"{NodeShape.DICT}" == "dict"
    assert str(NodeShape.SCALAR) == "scalar"


def test_path_token_kind_values_are_the_literal_strings() -> None:
    assert (
        PathTokenKind.KEY,
        PathTokenKind.KEY_EACH,
        PathTokenKind.KEY_WHOLE,
    ) == ("key", "key_each", "key_whole")


def test_shape_returns_node_shape_members() -> None:
    assert _shape({}) is NodeShape.DICT
    assert _shape([]) is NodeShape.LIST
    assert _shape("s") is NodeShape.SCALAR
    assert _shape(3) is NodeShape.SCALAR


def test_parse_path_dispatches_each_path_token_kind() -> None:
    """Every PathTokenKind is producible and correctly tagged by _parse_path."""
    assert _parse_path("a.b") == [
        (PathTokenKind.KEY, "a"),
        (PathTokenKind.KEY, "b"),
    ]
    assert _parse_path("a.b[*]") == [
        (PathTokenKind.KEY, "a"),
        (PathTokenKind.KEY_EACH, "b"),
    ]
    assert _parse_path("a.b[]") == [
        (PathTokenKind.KEY, "a"),
        (PathTokenKind.KEY_WHOLE, "b"),
    ]


def test_leaf_type_mismatch_message_byte_identical() -> None:
    """_check_leaf_type site (dict-vs-scalar shape clash)."""
    src = {"a": {"nested": "value"}}
    live = {"a": "scalar"}
    with pytest.raises(MergeTypeMismatch) as exc:
        overlay(src, live, ["a"])
    assert str(exc.value) == "type mismatch at 'a': src is dict, live is scalar"


def test_list_branch_mismatch_message_byte_identical() -> None:
    """_apply_overlay list branch (src non-list, live list, [] suffix)."""
    src = {"a": "x"}
    live = {"a": [1, 2]}
    with pytest.raises(MergeTypeMismatch) as exc:
        overlay(src, live, ["a[]"])
    assert str(exc.value) == "type mismatch at 'a[]': src is scalar, live is list"


def test_descend_non_mapping_message_byte_identical() -> None:
    """_apply_overlay non-mapping-descend site."""
    src = {"a": 5}
    live = {"a": {"b": 1}}
    with pytest.raises(MergeTypeMismatch) as exc:
        overlay(src, live, ["a.b"])
    assert str(exc.value) == "cannot descend into non-mapping at 'a.b'"


def test_deep_terminal_mismatch_message_byte_identical() -> None:
    """_apply_deep_overlay terminal shape-mismatch site."""
    src = {"a": 5}
    live = {"a": {"b": 1}}
    with pytest.raises(MergeTypeMismatch) as exc:
        overlay(src, live, [], deep_key_paths=["a"])
    assert (
        str(exc.value)
        == "deep-merge at 'a' requires dict on both sides; got src=scalar, live=dict"
    )


def test_deep_nested_mismatch_message_byte_identical() -> None:
    """_deep_merge_dicts nested shape-mismatch site."""
    src = {"a": {"b": 5}}
    live = {"a": {"b": {"c": 1}}}
    with pytest.raises(MergeTypeMismatch) as exc:
        overlay(src, live, [], deep_key_paths=["a"])
    assert str(exc.value) == "type mismatch at 'a.b': src is scalar, live is dict"
