"""Tests for my_setup.jsonc — JSONC overlay / strip / drift classify."""

from pathlib import Path

import pytest

from my_setup.errors import MergeTypeMismatch
from my_setup.jsonc import (
    classify_jsonc_drift,
    overlay_user_keys,
    parse_jsonc,
    strip_user_keys,
)


def test_parse_jsonc_strips_comments_and_returns_dict() -> None:
    text = """{
  // top comment
  "a": 1,  // inline
  /* block */
  "b": "two",
}"""
    assert parse_jsonc(text) == {"a": 1, "b": "two"}


def test_parse_jsonc_handles_trailing_comma() -> None:
    """JSONC + JSON5 accept trailing commas; stdlib json doesn't. The
    parser must not choke on the common VSCode-style trailing comma."""
    text = '{"a": 1,}'
    assert parse_jsonc(text) == {"a": 1}


def test_overlay_inserts_new_top_level_key_with_comments_intact() -> None:
    tracked = """{
  // top comment
  "existing": 1,  // inline a
  /* block before existing */
  "another": 2,
}
"""
    live = """{
  "existing": 1,
  "another": 2,
  "claudeCode.allowDangerouslySkipPermissions": true
}
"""
    out = overlay_user_keys(
        tracked, live, ["claudeCode.allowDangerouslySkipPermissions"]
    )
    assert "// top comment" in out
    assert "// inline a" in out
    assert "/* block before existing */" in out
    assert '"claudeCode.allowDangerouslySkipPermissions": true' in out


def test_overlay_replaces_existing_value_with_comments_intact() -> None:
    tracked = """{
  // top comment
  "claudeCode.initialPermissionMode": "default",  // inline
}
"""
    live = """{
  "claudeCode.initialPermissionMode": "bypassPermissions"
}
"""
    out = overlay_user_keys(tracked, live, ["claudeCode.initialPermissionMode"])
    assert '"claudeCode.initialPermissionMode": "bypassPermissions"' in out
    assert "// top comment" in out
    assert "// inline" in out
    assert '"default"' not in out


def test_overlay_skips_when_live_missing_key() -> None:
    tracked = """{
  "a": 1,
}
"""
    live = '{"a": 1}'
    out = overlay_user_keys(tracked, live, ["claudeCode.notInLive"])
    assert out == tracked


def test_overlay_handles_string_boolean_null_int_values() -> None:
    """Coverage of the supported scalar set on the python→model
    conversion path."""
    tracked = "{}\n"
    live = """{
  "s": "hi",
  "b": false,
  "n": null,
  "i": 42
}
"""
    out = overlay_user_keys(tracked, live, ["s", "b", "n", "i"])
    parsed = parse_jsonc(out)
    assert parsed == {"s": "hi", "b": False, "n": None, "i": 42}


def test_strip_removes_top_level_key_preserving_comments() -> None:
    text = """{
  // header comment
  "keep": 1,
  /* block before remove */
  "remove": true,  // inline
  "also-keep": "yes",
}
"""
    out = strip_user_keys(text, ["remove"])
    assert '"remove"' not in out
    assert "// header comment" in out
    assert '"keep": 1' in out
    assert '"also-keep": "yes"' in out


def test_strip_missing_key_is_noop() -> None:
    text = '{"a": 1}'
    assert strip_user_keys(text, ["missing"]) == text


def test_strip_only_key_yields_valid_empty_object() -> None:
    """Removing the sole key must leave a parseable empty object — the
    output may have residual whitespace but must round-trip through
    parse_jsonc as ``{}``."""
    text = '{"only": true}'
    out = strip_user_keys(text, ["only"])
    assert parse_jsonc(out) == {}


def test_strip_multiple_keys_in_one_call() -> None:
    text = """{
  "keep": 1,
  "drop1": "a",
  "drop2": "b",
  "also-keep": 2,
}
"""
    out = strip_user_keys(text, ["drop1", "drop2"])
    assert parse_jsonc(out) == {"keep": 1, "also-keep": 2}


def test_classify_drift_separates_expected_from_unexpected() -> None:
    src = '{"a": 1, "preserved": "tracked"}'
    live = '{"a": 99, "preserved": "live"}'
    expected, unexpected = classify_jsonc_drift(src, live, ["preserved"])
    assert expected == ["preserved"]
    assert unexpected == ["a"]


def test_classify_drift_returns_empty_when_no_divergence() -> None:
    src = '{"a": 1, "b": 2}'
    live = '{"a": 1, "b": 2}'
    expected, unexpected = classify_jsonc_drift(src, live, ["a"])
    assert expected == []
    assert unexpected == []


def test_classify_drift_treats_missing_key_as_drift() -> None:
    """Key absent on one side is divergence."""
    src = '{"a": 1}'
    live = '{"a": 1, "extra": true}'
    expected, unexpected = classify_jsonc_drift(src, live, ["extra"])
    assert expected == ["extra"]
    assert unexpected == []


# ---------------------------------------------------------------------------
# Deep-merge mode + lifted scalar restriction (dotfiles-nen.21)
# ---------------------------------------------------------------------------


def test_overlay_user_keys_deep_unions_top_level_object() -> None:
    """Deep mode unions sub-keys of a top-level object key, preserving
    comments on tracked's existing sub-keys."""
    tracked = '{\n  // model default\n  "claudeCode": {"model": "opus"}\n}\n'
    live = '{"claudeCode": {"fontSize": 14, "model": "opus"}}\n'
    out = overlay_user_keys(tracked, live, [], deep_key_names=["claudeCode"])
    parsed = parse_jsonc(out)
    assert parsed == {"claudeCode": {"model": "opus", "fontSize": 14}}
    assert "// model default" in out


def test_python_to_node_supports_nested_object() -> None:
    from json5.dumper import ModelDumper, dumps
    from json5.model import JSONObject

    from my_setup.jsonc import _python_to_node, parse_jsonc

    node = _python_to_node({"a": {"b": 1}})
    assert isinstance(node, JSONObject)
    # Round-trip via the model dumper: wrap in a Model? dumps takes a value too.
    text = dumps(node, dumper=ModelDumper())
    assert parse_jsonc(text) == {"a": {"b": 1}}


def test_python_to_node_supports_nested_array() -> None:
    from json5.dumper import ModelDumper, dumps
    from json5.model import JSONArray

    from my_setup.jsonc import _python_to_node, parse_jsonc

    node = _python_to_node([1, "two", {"a": 1}])
    assert isinstance(node, JSONArray)
    text = dumps(node, dumper=ModelDumper())
    assert parse_jsonc(text) == [1, "two", {"a": 1}]


def test_overlay_user_keys_shallow_now_handles_nested_object_values() -> None:
    """Shallow mode no longer raises on nested-object live values —
    the lifted scalar-only restriction means a top-level user-key with
    a nested object value writes as-is."""
    tracked = '{"a": 1}\n'
    live = '{"foo": {"nested": true}}\n'
    out = overlay_user_keys(tracked, live, ["foo"])
    parsed = parse_jsonc(out)
    assert parsed == {"a": 1, "foo": {"nested": True}}


def test_classify_jsonc_drift_treats_deep_keys_as_expected() -> None:
    src = '{"x": 1}'
    live = '{"x": 2}'
    expected, unexpected = classify_jsonc_drift(src, live, [], deep_key_names=["x"])
    assert expected == ["x"]
    assert unexpected == []


# ---------------------------------------------------------------------------
# Nested-path preserve_user_keys (dotfiles-nen.19)
# ---------------------------------------------------------------------------


def test_overlay_path_replaces_nested_leaf_in_tracked() -> None:
    """``"[python] > editor.fontSize"`` rewrites tracked's nested leaf with
    live's value while preserving sibling sub-keys and comments."""
    tracked = """{
  // python block
  "[python]": {
    "editor.defaultFormatter": "ruff",  // sibling preserved
    "editor.fontSize": 12
  }
}
"""
    live = """{
  "[python]": {
    "editor.defaultFormatter": "ruff",
    "editor.fontSize": 14
  }
}
"""
    out = overlay_user_keys(tracked, live, ["[python] > editor.fontSize"])
    assert parse_jsonc(out) == {
        "[python]": {"editor.defaultFormatter": "ruff", "editor.fontSize": 14}
    }
    assert "// python block" in out
    assert "// sibling preserved" in out


def test_overlay_path_appends_when_leaf_missing_in_tracked() -> None:
    """Case A — live has the nested leaf, tracked's intermediate exists
    but lacks the leaf: append a new KeyValuePair."""
    tracked = '{\n  "[python]": {\n    "editor.defaultFormatter": "ruff"\n  }\n}\n'
    live = (
        '{\n  "[python]": {\n'
        '    "editor.defaultFormatter": "ruff",\n'
        '    "editor.fontSize": 14\n  }\n}\n'
    )
    out = overlay_user_keys(tracked, live, ["[python] > editor.fontSize"])
    assert parse_jsonc(out) == {
        "[python]": {"editor.defaultFormatter": "ruff", "editor.fontSize": 14}
    }


def test_overlay_path_silent_when_live_missing_intermediate() -> None:
    """Live has no ``[python]`` block → overlay is a no-op for the path."""
    tracked = '{\n  "[python]": {\n    "editor.fontSize": 12\n  }\n}\n'
    live = '{"unrelated": 1}\n'
    out = overlay_user_keys(tracked, live, ["[python] > editor.fontSize"])
    # Tracked unchanged (parse-equal, since formatting may shift slightly).
    assert parse_jsonc(out) == parse_jsonc(tracked)


def test_overlay_path_silent_when_tracked_missing_intermediate() -> None:
    """Tracked has no ``[python]`` block → overlay does NOT auto-materialize."""
    tracked = '{"a": 1}\n'
    live = '{"[python]": {"editor.fontSize": 14}}\n'
    out = overlay_user_keys(tracked, live, ["[python] > editor.fontSize"])
    parsed = parse_jsonc(out)
    # No auto-materialize: tracked stays flat.
    assert "[python]" not in parsed


def test_overlay_path_raises_on_tracked_intermediate_type_mismatch() -> None:
    """Tracked has ``[python]`` as a scalar string instead of an object →
    MergeTypeMismatch with the path prefix in the message."""
    tracked = '{"[python]": "not-an-object"}\n'
    live = '{"[python]": {"editor.fontSize": 14}}\n'
    with pytest.raises(MergeTypeMismatch) as excinfo:
        overlay_user_keys(tracked, live, ["[python] > editor.fontSize"])
    assert "[python]" in str(excinfo.value)


def test_overlay_path_preserves_v1_flat_behavior_when_no_separator() -> None:
    """A single-segment ``preserve_user_keys`` entry continues to mean
    a literal top-level key (v1 behavior)."""
    tracked = '{"claudeCode.foo": false}\n'
    live = '{"claudeCode.foo": true}\n'
    out = overlay_user_keys(tracked, live, ["claudeCode.foo"])
    assert parse_jsonc(out) == {"claudeCode.foo": True}


def test_strip_path_removes_nested_leaf() -> None:
    """``"[python] > editor.fontSize"`` removes the leaf from tracked's
    nested object, leaving siblings intact."""
    tracked = """{
  "[python]": {
    "editor.defaultFormatter": "ruff",
    "editor.fontSize": 12
  }
}
"""
    out = strip_user_keys(tracked, ["[python] > editor.fontSize"])
    parsed = parse_jsonc(out)
    assert parsed == {"[python]": {"editor.defaultFormatter": "ruff"}}


def test_strip_path_leaves_empty_parent_intact() -> None:
    """Stripping the only sub-key leaves an empty ``[python]: {}`` object;
    pruning is out of scope."""
    tracked = '{"[python]": {"editor.fontSize": 12}}\n'
    out = strip_user_keys(tracked, ["[python] > editor.fontSize"])
    parsed = parse_jsonc(out)
    assert parsed == {"[python]": {}}


def test_strip_path_silent_when_tracked_missing_intermediate() -> None:
    """Tracked has no ``[python]`` → strip is a no-op for the path."""
    tracked = '{"a": 1}\n'
    out = strip_user_keys(tracked, ["[python] > editor.fontSize"])
    assert parse_jsonc(out) == {"a": 1}


def test_strip_path_silent_when_leaf_missing() -> None:
    """Tracked has ``[python]`` but no ``editor.fontSize`` → no-op."""
    tracked = '{"[python]": {"editor.defaultFormatter": "ruff"}}\n'
    out = strip_user_keys(tracked, ["[python] > editor.fontSize"])
    assert parse_jsonc(out) == {"[python]": {"editor.defaultFormatter": "ruff"}}


def test_classify_path_position_precise_expected() -> None:
    """Preserve path covers the only drift position → drift is expected."""
    src = '{"[python]": {"editor.defaultFormatter": "ruff", "editor.fontSize": 12}}'
    live = '{"[python]": {"editor.defaultFormatter": "ruff", "editor.fontSize": 14}}'
    expected, unexpected = classify_jsonc_drift(
        src, live, ["[python] > editor.fontSize"]
    )
    assert expected == ["[python]"]
    assert unexpected == []


def test_classify_path_position_precise_partial_overlap_is_unexpected() -> None:
    """Preserve path covers one sub-key; another sub-key also drifts →
    top-level is unexpected (position-precise; no silent absorb)."""
    src = '{"[python]": {"editor.defaultFormatter": "ruff", "editor.fontSize": 12}}'
    live = '{"[python]": {"editor.defaultFormatter": "black", "editor.fontSize": 14}}'
    expected, unexpected = classify_jsonc_drift(
        src, live, ["[python] > editor.fontSize"]
    )
    assert expected == []
    assert unexpected == ["[python]"]


def test_classify_path_position_precise_unrelated_top_level_drift() -> None:
    """A preserve path under one top-level key does NOT make a different
    top-level key's drift expected."""
    src = '{"[python]": {"editor.fontSize": 12}, "other": 1}'
    live = '{"[python]": {"editor.fontSize": 14}, "other": 99}'
    expected, unexpected = classify_jsonc_drift(
        src, live, ["[python] > editor.fontSize"]
    )
    assert "[python]" in expected
    assert "other" in unexpected


def test_classify_path_missing_in_live_treated_as_position_drift() -> None:
    """When the preserve path's leaf is missing on one side, that's drift
    at the leaf position — covered by the preserve set → expected."""
    src = '{"[python]": {"editor.fontSize": 12}}'
    live = '{"[python]": {}}'
    expected, unexpected = classify_jsonc_drift(
        src, live, ["[python] > editor.fontSize"]
    )
    assert expected == ["[python]"]
    assert unexpected == []


def test_overlay_path_v1_top_level_with_dot_in_name_still_literal() -> None:
    """A v1-style literal name like ``claudeCode.foo`` (no ` > `) keeps
    treating the dot as part of the key name — the path syntax only
    splits on ` > ` (space-arrow-space)."""
    tracked = '{"claudeCode.foo": false}\n'
    live = '{"claudeCode.foo": true}\n'
    out = overlay_user_keys(tracked, live, ["claudeCode.foo"])
    assert parse_jsonc(out) == {"claudeCode.foo": True}


# ---------------------------------------------------------------------------
# Pydantic validator on preserve_user_keys (dotfiles-nen.19)
# ---------------------------------------------------------------------------


def test_pydantic_rejects_empty_path_segment() -> None:
    """Leading ``" > "`` produces an empty first segment → reject."""
    from pydantic import ValidationError

    from my_setup.config import Dotfile

    with pytest.raises(ValidationError):
        Dotfile(src=Path("a.json"), dst="/tmp/a.json", preserve_user_keys=[" > foo"])


def test_pydantic_rejects_trailing_separator() -> None:
    """Trailing ``" > "`` produces an empty last segment → reject."""
    from pydantic import ValidationError

    from my_setup.config import Dotfile

    with pytest.raises(ValidationError):
        Dotfile(src=Path("a.json"), dst="/tmp/a.json", preserve_user_keys=["foo > "])


def test_pydantic_rejects_whitespace_only_segment() -> None:
    """A segment that's just whitespace is rejected as malformed."""
    from pydantic import ValidationError

    from my_setup.config import Dotfile

    with pytest.raises(ValidationError):
        Dotfile(
            src=Path("a.json"), dst="/tmp/a.json", preserve_user_keys=["foo >    > bar"]
        )


def test_pydantic_rejects_empty_string_path() -> None:
    """An empty path string is rejected."""
    from pydantic import ValidationError

    from my_setup.config import Dotfile

    with pytest.raises(ValidationError):
        Dotfile(src=Path("a.json"), dst="/tmp/a.json", preserve_user_keys=[""])


def test_pydantic_rejects_head_collision_with_deep_list() -> None:
    """A nested path whose head segment is also in
    ``preserve_user_keys_deep`` is rejected (conflicting semantics:
    whole-subtree vs leaf)."""
    from pydantic import ValidationError

    from my_setup.config import Dotfile

    with pytest.raises(ValidationError):
        Dotfile(
            src=Path("a.json"),
            dst="/tmp/a.json",
            preserve_user_keys=["[python] > editor.fontSize"],
            preserve_user_keys_deep=["[python]"],
        )


def test_pydantic_accepts_well_formed_nested_path() -> None:
    """Sanity check: a clean two-segment path is accepted."""
    from my_setup.config import Dotfile

    dotfile = Dotfile(
        src=Path("a.json"),
        dst="/tmp/a.json",
        preserve_user_keys=["[python] > editor.fontSize"],
    )
    assert dotfile.preserve_user_keys == ["[python] > editor.fontSize"]


def test_pydantic_accepts_flat_v1_paths() -> None:
    """Sanity check: single-segment names (no separator) still accepted."""
    from my_setup.config import Dotfile

    dotfile = Dotfile(
        src=Path("a.json"),
        dst="/tmp/a.json",
        preserve_user_keys=["claudeCode.foo"],
    )
    assert dotfile.preserve_user_keys == ["claudeCode.foo"]
