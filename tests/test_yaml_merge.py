"""Tests for yaml_merge.overlay / extract_keys / delete_keys."""

import pytest

from my_setup.errors import MergeTypeMismatch
from my_setup.yaml_merge import delete_keys, extract_keys, overlay


def test_dotted_path_overlay() -> None:
    src = {"a": {"b": {"c": 1, "d": 99}}}
    live = {"a": {"b": {"c": 2}}}
    merged = overlay(src, live, ["a.b.c"])
    assert merged == {"a": {"b": {"c": 2, "d": 99}}}


def test_path_absent_in_live_keeps_src() -> None:
    src = {"a": {"b": 1}}
    live = {"a": {}}
    merged = overlay(src, live, ["a.b"])
    assert merged == {"a": {"b": 1}}


def test_path_absent_in_src_adds_live_key() -> None:
    src = {"a": {}}
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


def test_extract_keys_returns_present_paths() -> None:
    doc = {"a": {"b": 42}, "c": [1, 2, 3]}
    assert extract_keys(doc, ["a.b", "c[]"]) == {"a.b": 42, "c[]": [1, 2, 3]}


def test_extract_keys_skips_missing() -> None:
    doc = {"a": {"b": 1}}
    assert extract_keys(doc, ["a.b", "missing.path"]) == {"a.b": 1}


def test_delete_keys_removes_present_paths() -> None:
    doc = {"a": {"b": 1, "c": 2}, "d": 3}
    delete_keys(doc, ["a.b", "d"])
    assert doc == {"a": {"c": 2}}


def test_delete_keys_skips_missing() -> None:
    doc = {"a": 1}
    delete_keys(doc, ["does.not.exist"])
    assert doc == {"a": 1}


def test_invalid_path_raises_value_error() -> None:
    with pytest.raises(ValueError):
        overlay({}, {}, [".bad"])


def test_list_suffix_only_at_end() -> None:
    with pytest.raises(ValueError, match="only allowed at end"):
        overlay({}, {}, ["a[*].b"])


def test_multiple_paths_compose() -> None:
    src = {"a": 1, "b": {"c": 2, "d": 3}, "e": [1, 2]}
    live = {"a": 10, "b": {"c": 20}, "e": [99, 88, 77]}
    merged = overlay(src, live, ["a", "b.c", "e[]"])
    assert merged == {"a": 10, "b": {"c": 20, "d": 3}, "e": [99, 88, 77]}
