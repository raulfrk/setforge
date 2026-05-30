"""Tests for yaml_merge.overlay / extract_keys / delete_keys."""

import pytest

from setforge.errors import MergeTypeMismatch
from setforge.yaml_merge import delete_keys, extract_keys, overlay


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
