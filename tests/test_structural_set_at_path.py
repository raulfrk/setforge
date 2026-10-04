"""Tests for comment-preserving set-value-at-path on the structural merge model.

``set_at_path`` is the rebuild seam for the take-tracked disposition: after the
3-way structural merge records a :class:`PathConflict`, the install driver writes
the chosen (tracked / theirs) value back at that conflict's dotted path. The
write must preserve sibling comments and the replaced leaf's own preceding
whitespace/comment on a ruamel YAML model and a plain dict, never auto-vivify a
missing parent, and refuse a json-five parent (JSON is staged whole-document).
"""

import copy
import io
from collections.abc import Mapping

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from json5.dumper import ModelDumper
from json5.dumper import dumps as json5_dumps
from json5.loader import ModelLoader
from json5.loader import loads as json5_loads
from ruamel.yaml import YAML

from setforge.errors import MergeTypeMismatch
from setforge.structural_merge import (
    delete_node_at_path,
    set_at_path,
    set_node_at_path,
)


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


def test_plain_dict_set_list_value() -> None:
    """A list value lands on a plain dict."""
    doc: dict[str, object] = {"a": 1}
    set_at_path(doc, "items", [1, 2, 3])
    assert doc == {"a": 1, "items": [1, 2, 3]}


# ---------------------------------------------------------------------------
# 4. A json-five parent is refused: JSON files are staged as one whole-document
#    unit, so nothing writes into them by path.
# ---------------------------------------------------------------------------


_JSON_DOC = '{\n  "a": 1, // keep me\n  "b": {"n": 2}\n}\n'


def test_jsonc_set_at_path_refuses_and_leaves_document_unchanged() -> None:
    model = _jload(_JSON_DOC)
    with pytest.raises(
        MergeTypeMismatch,
        match=r"^cannot set leaf at 'c': parent is JSONObject, not a mapping$",
    ):
        set_at_path(model, "c", 3)
    assert _jdump(model) == _JSON_DOC


def test_jsonc_set_node_at_path_refuses_and_leaves_document_unchanged() -> None:
    model = _jload(_JSON_DOC)
    with pytest.raises(
        MergeTypeMismatch,
        match=r"^cannot set node at 'b\.n': parent is JSONObject, not a mapping$",
    ):
        set_node_at_path(model, "b.n", {"x": 1})
    assert _jdump(model) == _JSON_DOC


def test_jsonc_delete_node_at_path_refuses_and_leaves_document_unchanged() -> None:
    model = _jload(_JSON_DOC)
    with pytest.raises(
        MergeTypeMismatch,
        match=r"^cannot delete leaf at 'a': parent is JSONObject, not a mapping$",
    ):
        delete_node_at_path(model, "a")
    assert _jdump(model) == _JSON_DOC


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


# ---------------------------------------------------------------------------
# 7. set_node_at_path: splice a WRAPPED subtree node (comments-on-the-node
#    preserved), the comment-preserving whole-subtree re-assert seam.
# ---------------------------------------------------------------------------


def _ynode_at(doc: object, key: str) -> object:
    """Return the still-wrapped child node at top-level ``key``."""
    assert isinstance(doc, Mapping)
    return doc[key]


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


def test_set_node_missing_parent_raises_keyerror() -> None:
    """A nested node splice whose parent is absent raises KeyError."""
    src = _yload("a:\n  b: 1\n")
    node = copy.deepcopy(_ynode_at(src, "a"))
    dst = _yload("a:\n  b: 1\n")
    with pytest.raises(KeyError):
        set_node_at_path(dst, "missing.child", node)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tail\\", ["tail\\"]),
        (r"\.", ["."]),
        (r"\\", ["\\"]),
        (r"\0", [""]),
        (r"\0.\0", ["", ""]),
    ],
)
def test_key_path_legacy_terminal_escape_and_literal_empty_segments(
    path: str, expected: list[str]
) -> None:
    from setforge.structural_merge import split_key_path

    assert split_key_path(path) == expected


@pytest.mark.parametrize("container", ["mapping", "sequence"])
def test_splicing_yaml_subtree_deduplicates_anchor_names_and_preserves_aliases(
    container: str,
) -> None:
    from setforge.structural_merge import get_node_at_path

    target = _yload(
        "existing: &shared\n  value: 9\nexisting_alias: *shared\npromoted: 0\n"
    )
    live = _yload(
        "promoted:\n  first: &shared\n    value: 1\n  second: *shared\n"
        "  independent: &distinct\n    value: 2\n  independent_alias: *distinct\n"
        if container == "mapping"
        else "promoted:\n  - &shared\n    value: 1\n  - *shared\n"
        "  - &distinct\n    value: 2\n  - *distinct\n"
    )
    node = get_node_at_path(live, "promoted")
    set_node_at_path(target, "promoted", node)
    output = _ydump(target)
    assert output.count("&shared") == 1
    assert output.count("&distinct") == 1
    reloaded = _yload(output)
    assert isinstance(reloaded, Mapping)
    assert reloaded["existing"] is reloaded["existing_alias"]
    assert reloaded["existing"]["value"] == 9
    promoted = reloaded["promoted"]
    first = promoted["first"] if container == "mapping" else promoted[0]
    second = promoted["second"] if container == "mapping" else promoted[1]
    assert first is second
    assert first["value"] == 1


def test_splicing_existing_yaml_node_retains_anchor_through_sequence_aliases() -> None:
    model = _yload("held: &shared\n  value: 1\naliases:\n  - *shared\n  - *shared\n")
    assert isinstance(model, Mapping)
    before = _ydump(model)
    set_node_at_path(model, "held", model["held"])
    assert _ydump(model) == before
    reloaded = _yload(_ydump(model))
    assert isinstance(reloaded, Mapping)
    assert reloaded["held"] is reloaded["aliases"][0]
    assert reloaded["aliases"][0] is reloaded["aliases"][1]


@given(st.text(max_size=64))
@example("XX[XX")
@example("XX]XX")
@example(r"name\[].child[*]")
def test_literal_key_codec_round_trips_identity(key: str) -> None:
    from setforge.structural_merge import encode_key_segment, split_key_path

    assert split_key_path(encode_key_segment(key)) == [key]
