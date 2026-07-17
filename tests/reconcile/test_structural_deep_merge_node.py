"""Direct unit tests for :func:`setforge.structural_merge.deep_merge_into_node`.

The comment-preserving sibling of :func:`setforge.yaml_merge._deep_merge_dicts`:
it deep-merges a PLAIN ``live`` mapping OVER a still-WRAPPED backend node (ruamel
``CommentedMap`` / json-five ``JSONObject``) in place, so every untouched key —
and the comment tokens attached to it — survives. The caller deep-copies the
wrapped node, merges live over it, then splices the result back via
:func:`setforge.structural_merge.set_node_at_path`; these tests mirror that
sequence directly (node construction mirrors ``test_structural_set_at_path.py``).

Each of the five documented branches is exercised with a BITING assertion (the
merged structure/values are checked, not merely "no exception"), including the
self-recursive shared-mapping branch so a mutation there survives no test.
"""

import copy
import io
from collections.abc import Mapping
from typing import cast

import pytest
from json5.dumper import ModelDumper
from json5.dumper import dumps as json5_dumps
from json5.loader import ModelLoader
from json5.loader import loads as json5_loads
from json5.model import JSONObject
from ruamel.yaml import YAML

from setforge.errors import MergeTypeMismatch
from setforge.structural_merge import deep_merge_into_node


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


def _ynode_at(doc: object, key: str) -> Mapping:
    """Return the still-wrapped ruamel child mapping node at top-level ``key``."""
    assert isinstance(doc, Mapping)
    child = doc[key]
    assert isinstance(child, Mapping)
    return child


def _jload(text: str) -> object:
    return json5_loads(text, loader=ModelLoader())


def _jdump(node: object) -> str:
    return json5_dumps(node, dumper=ModelDumper())


def _jtop(model: object) -> JSONObject:
    top = model if isinstance(model, JSONObject) else getattr(model, "value", model)
    assert isinstance(top, JSONObject)
    return top


def _jnode_at(model: object, key: str) -> object:
    """Return the still-wrapped json-five value node at top-level ``key``."""
    top = _jtop(model)
    idx = next(
        i for i, k in enumerate(top.keys) if getattr(k, "characters", None) == key
    )
    return top.values[idx]


# ---------------------------------------------------------------------------
# YAML backend: all five branches in one merge, comments preserved throughout.
# ---------------------------------------------------------------------------


def test_yaml_deep_merge_all_branches_preserving_comments() -> None:
    """One merge exercises: live-only add, recurse-into-shared-mapping (the
    self-recursive branch), whole-list replace, and in-place scalar take —
    while every untouched key's comment tokens survive byte-visibly."""
    src = _yload(
        "root:\n"
        "  keep: 1        # keep comment\n"
        "  nested:\n"
        "    a: 1         # a comment\n"
        "    b: 2         # b comment\n"
        "  lst:\n"
        "    - x\n"
    )
    target = copy.deepcopy(_ynode_at(src, "root"))
    live = {
        "keep": 1,  # shared scalar, unchanged
        "nested": {"a": 9, "c": 3},  # recurse: change a, add c, leave b untouched
        "lst": ["y", "z"],  # whole-list replace
        "brandnew": "hi",  # live-only key added
    }

    deep_merge_into_node(target, live)

    nested = cast(Mapping, target["nested"])
    # In-place scalar take: a changed 1 -> 9, its own comment survives.
    assert nested["a"] == 9
    # Untouched-by-live nested key is left byte-identical (value AND comment).
    assert nested["b"] == 2
    # Recursion added the live-only nested key.
    assert nested["c"] == 3
    # Whole-list replace: live's list wins outright.
    assert list(cast(list, target["lst"])) == ["y", "z"]
    # Live-only top-level key added.
    assert target["brandnew"] == "hi"
    # Shared unchanged scalar stays.
    assert target["keep"] == 1

    out = _ydump(target)
    assert "# keep comment" in out
    assert "# a comment" in out  # survived the in-place scalar take
    assert "# b comment" in out  # untouched key, comment byte-identical


def test_yaml_deep_merge_recurses_arbitrarily_deep() -> None:
    """The self-recursive branch threads down multiple mapping levels: a deep
    leaf change lands and a deep untouched sibling's comment survives."""
    src = _yload(
        "root:\n"
        "  l1:\n"
        "    l2:\n"
        "      deep: 1    # deep comment\n"
        "      sib: 2     # sib comment\n"
    )
    target = copy.deepcopy(_ynode_at(src, "root"))

    deep_merge_into_node(target, {"l1": {"l2": {"deep": 42, "added": 7}}})

    l2 = cast(Mapping, cast(Mapping, target["l1"])["l2"])
    assert l2["deep"] == 42
    assert l2["sib"] == 2  # untouched deep sibling
    assert l2["added"] == 7  # added at depth 3
    out = _ydump(target)
    assert "# deep comment" in out
    assert "# sib comment" in out


# ---------------------------------------------------------------------------
# json-five (JSONC) backend: recurse + add + comment survival.
# ---------------------------------------------------------------------------


def test_jsonc_deep_merge_recurses_and_preserves_comments() -> None:
    """Merging over a wrapped JSONObject keeps interior // comments and threads
    the recursion into a shared nested object."""
    src = _jload(
        '{\n  "root": {\n    "a": 1, // a comment\n    "nested": { "x": 1 }\n  }\n}\n'
    )
    target = copy.deepcopy(_jnode_at(src, "root"))

    deep_merge_into_node(target, {"a": 5, "nested": {"x": 9, "y": 2}, "new": 7})

    out = _jdump(target)
    assert "// a comment" in out
    # Round-trip parse confirms the merged structure/values.
    assert json5_loads(out) == {"a": 5, "nested": {"x": 9, "y": 2}, "new": 7}


# ---------------------------------------------------------------------------
# Type-mismatch branch: scalar-vs-mapping shape clash raises with the path.
# ---------------------------------------------------------------------------


def test_deep_merge_scalar_over_mapping_raises_type_mismatch() -> None:
    """A live scalar landing on a shared mapping node is a shape clash: it
    raises MergeTypeMismatch naming the dotted sub-path."""
    src = _jload('{"root": {"m": {"k": 1}}}')
    target = copy.deepcopy(_jnode_at(src, "root"))

    with pytest.raises(MergeTypeMismatch, match=r"'m'"):
        deep_merge_into_node(target, {"m": "scalar"})


def test_deep_merge_list_over_scalar_raises_type_mismatch() -> None:
    """A live list landing on a shared scalar (neither the list-vs-list nor the
    mapping-vs-mapping fast path) falls through to the shape check and raises."""
    src = _yload("root:\n  s: 1\n")
    target = copy.deepcopy(_ynode_at(src, "root"))

    with pytest.raises(MergeTypeMismatch, match=r"'s'"):
        deep_merge_into_node(target, {"s": [1, 2, 3]})
