"""Tests for comment-preserving set-value-at-path on the structural merge model.

``set_at_path`` is the rebuild seam for the take-tracked disposition: after the
3-way structural merge records a :class:`PathConflict`, the install driver writes
the chosen (tracked / theirs) value back at that conflict's dotted path. The
write must preserve sibling comments and the replaced leaf's own preceding
whitespace/comment across all three backends (ruamel YAML, json-five JSONC,
plain dict), and never auto-vivify a missing parent.
"""

import copy
import io
from collections.abc import Mapping

import pytest
from json5.dumper import ModelDumper
from json5.dumper import dumps as json5_dumps
from json5.loader import ModelLoader
from json5.loader import loads as json5_loads
from json5.model import JSONObject
from ruamel.yaml import YAML

from setforge.structural_merge import set_at_path, set_node_at_path


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


def _jtop(model: object) -> JSONObject:
    """Return the top ``JSONObject`` for a loaded json-five model (unwraps text)."""
    top = model if isinstance(model, JSONObject) else getattr(model, "value", model)
    assert isinstance(top, JSONObject)
    return top


# ---------------------------------------------------------------------------
# 1. New scalar leaf at an existing parent; siblings' comments survive.
# ---------------------------------------------------------------------------


def test_yaml_new_leaf_preserves_sibling_comments() -> None:
    """A new YAML leaf appears; other keys' comments survive byte-for-byte."""
    doc = _yload("a: 1  # keep me\nb: 2  # also keep\n")
    set_at_path(doc, "c", 3)
    out = _ydump(doc)
    assert "# keep me" in out
    assert "# also keep" in out
    assert "c: 3" in out


def test_jsonc_new_leaf_preserves_sibling_comments() -> None:
    """A new JSONC leaf appears; sibling comments survive."""
    model = _jload('{\n  "a": 1, // keep me\n  "b": 2 // also keep\n}\n')
    set_at_path(model, "c", 3)
    out = _jdump(model)
    assert "// keep me" in out
    assert "// also keep" in out
    assert '"c"' in out


def test_plain_dict_new_leaf() -> None:
    """A new leaf lands on a plain dict."""
    doc: dict[str, object] = {"a": 1, "b": 2}
    set_at_path(doc, "c", 3)
    assert doc == {"a": 1, "b": 2, "c": 3}


# ---------------------------------------------------------------------------
# 2. Replace an existing scalar leaf; the leaf's own preceding comment survives.
# ---------------------------------------------------------------------------


def test_yaml_replace_leaf_preserves_own_comment() -> None:
    """Replacing a YAML leaf keeps its trailing same-line comment."""
    doc = _yload("a: 1  # leaf comment\nb: 2\n")
    set_at_path(doc, "a", 99)
    out = _ydump(doc)
    assert "a: 99" in out
    assert "# leaf comment" in out


def test_jsonc_replace_leaf_preserves_wsc_before() -> None:
    """Replacing a JSONC leaf preserves its wsc_before (leading whitespace)."""
    model = _jload('{\n  "a": 1,\n  "b": 2\n}\n')
    parent = _jtop(model)
    idx = next(
        i for i, k in enumerate(parent.keys) if getattr(k, "characters", None) == "a"
    )
    before = list(parent.values[idx].wsc_before)
    set_at_path(model, "a", 99)
    idx2 = next(
        i for i, k in enumerate(parent.keys) if getattr(k, "characters", None) == "a"
    )
    assert list(parent.values[idx2].wsc_before) == before
    out = _jdump(model)
    assert '"a": 99' in out


def test_plain_dict_replace_leaf() -> None:
    """Replacing a leaf on a plain dict sets the new value."""
    doc: dict[str, object] = {"a": 1, "b": 2}
    set_at_path(doc, "a", 99)
    assert doc == {"a": 99, "b": 2}


# ---------------------------------------------------------------------------
# 3. Set a LIST value (the take-tracked-a-list case).
# ---------------------------------------------------------------------------


def test_yaml_set_list_value() -> None:
    """A list value dumps as a YAML sequence."""
    doc = _yload("a: 1\n")
    set_at_path(doc, "items", [1, 2, 3])
    out = _ydump(doc)
    reloaded = _yload(out)
    assert isinstance(reloaded, Mapping)
    assert list(reloaded["items"]) == [1, 2, 3]


def test_jsonc_set_list_value() -> None:
    """A list value dumps as a JSONC array."""
    model = _jload('{\n  "a": 1\n}\n')
    set_at_path(model, "items", [1, 2, 3])
    out = _jdump(model)
    reloaded = json5_loads(out)
    assert reloaded["items"] == [1, 2, 3]


def test_plain_dict_set_list_value() -> None:
    """A list value lands on a plain dict."""
    doc: dict[str, object] = {"a": 1}
    set_at_path(doc, "items", [1, 2, 3])
    assert doc == {"a": 1, "items": [1, 2, 3]}


# ---------------------------------------------------------------------------
# 4. json-five keys/values stay length-consistent (spliced, not derived prop).
# ---------------------------------------------------------------------------


def test_jsonc_keys_values_consistent_after_set() -> None:
    """After a set the parent's .keys and .values stay equal-length."""
    model = _jload('{\n  "a": 1,\n  "b": 2\n}\n')
    set_at_path(model, "c", 3)
    parent = _jtop(model)
    assert len(parent.keys) == len(parent.values)
    assert len(parent.keys) == 3
    # Replace too -> still consistent.
    set_at_path(model, "a", 7)
    assert len(parent.keys) == len(parent.values)


# ---------------------------------------------------------------------------
# 5. Missing intermediate parent -> KeyError (no auto-vivification).
# ---------------------------------------------------------------------------


def test_yaml_missing_parent_raises_keyerror() -> None:
    """A nested write whose parent is absent raises KeyError, not vivify."""
    doc = _yload("a: 1\n")
    with pytest.raises(KeyError):
        set_at_path(doc, "missing.child", 1)


def test_jsonc_missing_parent_raises_keyerror() -> None:
    """A nested JSONC write whose parent is absent raises KeyError."""
    model = _jload('{\n  "a": 1\n}\n')
    with pytest.raises(KeyError):
        set_at_path(model, "missing.child", 1)


def test_plain_dict_missing_parent_raises_keyerror() -> None:
    """A nested plain-dict write whose parent is absent raises KeyError."""
    doc: dict[str, object] = {"a": 1}
    with pytest.raises(KeyError):
        set_at_path(doc, "missing.child", 1)


# ---------------------------------------------------------------------------
# 6. Byte-stable: setting an existing key to its same value leaves the rest of
#    the document's dump unchanged (anchors / quotes / comments preserved).
# ---------------------------------------------------------------------------


def test_yaml_noop_set_is_byte_stable() -> None:
    """Setting an existing key to its same value leaves the dump unchanged."""
    text = 'a: "quoted"  # c1\nb:\n  - 1\n  - 2\nc: 3  # c3\n'
    doc = _yload(text)
    before = _ydump(doc)
    set_at_path(doc, "c", 3)
    after = _ydump(doc)
    assert after == before


def test_jsonc_set_existing_keeps_siblings_byte_stable() -> None:
    """Replacing one JSONC leaf leaves the other members' bytes unchanged."""
    text = '{\n  "a": 1, // c1\n  "b": 2, // c2\n  "c": 3 // c3\n}\n'
    model = _jload(text)
    set_at_path(model, "b", 2)
    out = _jdump(model)
    assert "// c1" in out
    assert "// c2" in out
    assert "// c3" in out
    assert '"a": 1' in out
    assert '"c": 3' in out


# ---------------------------------------------------------------------------
# 7. set_node_at_path: splice a WRAPPED subtree node (comments-on-the-node
#    preserved), the comment-preserving whole-subtree re-assert seam.
# ---------------------------------------------------------------------------


def _ynode_at(doc: object, key: str) -> object:
    """Return the still-wrapped child node at top-level ``key``."""
    assert isinstance(doc, Mapping)
    return doc[key]


def _jnode_at(model: object, key: str) -> object:
    """Return the still-wrapped json-five value node at top-level ``key``."""
    top = _jtop(model)
    idx = next(
        i for i, k in enumerate(top.keys) if getattr(k, "characters", None) == key
    )
    return top.values[idx]


def test_yaml_set_node_preserves_subtree_internal_comments() -> None:
    """Splicing a wrapped CommentedMap carries its OWN interior comments."""
    src = _yload("pinned:\n  x: 1  # x comment\n  y: 2  # y comment\nother: keep\n")
    node = copy.deepcopy(_ynode_at(src, "pinned"))
    dst = _yload("pinned:\n  x: 9\nother: keep\n")
    set_node_at_path(dst, "pinned", node)
    out = _ydump(dst)
    assert "# x comment" in out
    assert "# y comment" in out
    assert "other: keep" in out


def test_jsonc_set_node_preserves_subtree_internal_comments() -> None:
    """Splicing a wrapped JSONObject carries its OWN interior // comments."""
    src = _jload(
        '{\n  "pinned": {\n    "x": 1, // x comment\n    "y": 2 // y comment\n  },\n'
        '  "other": "keep"\n}\n'
    )
    node = copy.deepcopy(_jnode_at(src, "pinned"))
    dst = _jload('{\n  "pinned": {\n    "x": 9\n  },\n  "other": "keep"\n}\n')
    set_node_at_path(dst, "pinned", node)
    out = _jdump(dst)
    assert "// x comment" in out
    assert "// y comment" in out
    assert '"other": "keep"' in out
    # keys / values stayed in lockstep (the derived zip re-reads them on dump).
    top = _jtop(dst)
    assert len(top.keys) == len(top.values)


def test_jsonc_set_node_keys_values_stay_in_lockstep() -> None:
    """A whole-value swap edits parent.keys[idx]/parent.values[idx] in lockstep."""
    src = _jload('{\n  "a": {"n": 1},\n  "b": 2,\n  "c": 3\n}\n')
    node = copy.deepcopy(_jnode_at(src, "a"))
    dst = _jload('{\n  "a": {"n": 9},\n  "b": 2,\n  "c": 3\n}\n')
    set_node_at_path(dst, "a", node)
    top = _jtop(dst)
    assert len(top.keys) == len(top.values) == 3
    out = _jdump(dst)
    assert json5_loads(out) == {"a": {"n": 1}, "b": 2, "c": 3}


def test_yaml_set_node_dedups_colliding_anchor() -> None:
    """A swapped node whose anchor name collides with a DIFFERENT target node
    is dedup'd so the dump carries no duplicate anchor definition."""
    src = _yload("pinned: &shared\n  x: 1\nother: keep\n")
    node = copy.deepcopy(_ynode_at(src, "pinned"))  # carries &shared
    dst = _yload("pinned:\n  x: 9\nelsewhere: &shared\n  z: 5\nref: *shared\n")
    set_node_at_path(dst, "pinned", node)
    out = _ydump(dst)
    # The swapped node must NOT re-emit a second `&shared` definition.
    assert out.count("&shared") == 1
    # Re-parsing must not raise a duplicate-anchor / reused-anchor error.
    _yload(out)


def test_yaml_set_node_byte_stable_on_noop() -> None:
    """Swapping a node for a deep-copy of the identical node is byte-stable."""
    text = "pinned:\n  x: 1  # x comment\n  y: 2  # y comment\nother: keep\n"
    dst = _yload(text)
    before = _ydump(dst)
    node = copy.deepcopy(_ynode_at(dst, "pinned"))
    set_node_at_path(dst, "pinned", node)
    after = _ydump(dst)
    assert after == before


def test_yaml_set_node_anchored_subtree_byte_stable_on_noop() -> None:
    """A pinned subtree carrying an ``&anchor`` and the ``*alias`` that references
    it survives a no-op swap byte-identical — the pair is NOT flattened.

    The ``&shared`` definition and the ``*shared`` alias both live inside the
    pinned subtree, so :func:`copy.deepcopy` keeps them the same object (the
    snapshot the re-assert seam captures). But the target's pinned slot still
    carries the SAME ``&shared`` name, so the dedup's existing-anchor walk finds
    it and — unless the slot being replaced is excluded — clears the copy's own
    anchor. ruamel then re-synthesizes opaque ``&id001`` / ``*id001`` names,
    flattening the original ``&shared`` / ``*shared`` pair on every deploy. The
    fix excludes the slot under replacement, so the named pair round-trips
    byte-identical.
    """
    text = (
        "pinned:\n  base: &shared\n    x: 1\n    y: 2\n  user: *shared\nother: keep\n"
    )
    dst = _yload(text)
    before = _ydump(dst)
    node = copy.deepcopy(_ynode_at(dst, "pinned"))  # carries &shared + *shared
    set_node_at_path(dst, "pinned", node)
    after = _ydump(dst)
    assert after == before
    assert "&shared" in after
    assert "*shared" in after


def test_jsonc_set_node_byte_stable_on_noop() -> None:
    """Swapping a json-five node for a deep-copy of itself is byte-stable."""
    text = '{\n  "pinned": {\n    "x": 1 // x comment\n  },\n  "other": "keep"\n}\n'
    dst = _jload(text)
    before = _jdump(dst)
    node = copy.deepcopy(_jnode_at(dst, "pinned"))
    set_node_at_path(dst, "pinned", node)
    after = _jdump(dst)
    assert after == before


def test_set_node_missing_parent_raises_keyerror() -> None:
    """A nested node splice whose parent is absent raises KeyError."""
    src = _yload("a:\n  b: 1\n")
    node = copy.deepcopy(_ynode_at(src, "a"))
    dst = _yload("a:\n  b: 1\n")
    with pytest.raises(KeyError):
        set_node_at_path(dst, "missing.child", node)
