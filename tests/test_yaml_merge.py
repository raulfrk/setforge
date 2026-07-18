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
