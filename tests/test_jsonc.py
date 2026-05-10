"""Tests for my_setup.jsonc — JSONC overlay / strip / drift classify."""

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
