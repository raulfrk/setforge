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
    top = _jtop(model)
    idx = next(
        i for i, k in enumerate(top.keys) if getattr(k, "characters", None) == key
    )
    return top.values[idx]


def test_yaml_deep_merge_all_branches_preserving_comments() -> None:
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
        "keep": 1,
        "nested": {"a": 9, "c": 3},
        "lst": ["y", "z"],
        "brandnew": "hi",
    }

    deep_merge_into_node(target, live)

    nested = cast(Mapping, target["nested"])
    assert nested["a"] == 9
    assert nested["b"] == 2
    assert nested["c"] == 3
    assert list(cast(list, target["lst"])) == ["y", "z"]
    assert target["brandnew"] == "hi"
    assert target["keep"] == 1

    out = _ydump(target)
    assert "# keep comment" in out
    assert "# a comment" in out
    assert "# b comment" in out


def test_yaml_deep_merge_recurses_arbitrarily_deep() -> None:
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
    assert l2["sib"] == 2
    assert l2["added"] == 7
    out = _ydump(target)
    assert "# deep comment" in out
    assert "# sib comment" in out


def test_jsonc_deep_merge_recurses_and_preserves_comments() -> None:
    src = _jload(
        '{\n  "root": {\n    "a": 1, // a comment\n    "nested": { "x": 1 }\n  }\n}\n'
    )
    target = copy.deepcopy(_jnode_at(src, "root"))

    deep_merge_into_node(target, {"a": 5, "nested": {"x": 9, "y": 2}, "new": 7})

    out = _jdump(target)
    assert "// a comment" in out
    assert json5_loads(out) == {"a": 5, "nested": {"x": 9, "y": 2}, "new": 7}


def test_deep_merge_scalar_over_mapping_raises_type_mismatch() -> None:
    src = _jload('{"root": {"m": {"k": 1}}}')
    target = copy.deepcopy(_jnode_at(src, "root"))

    with pytest.raises(MergeTypeMismatch, match=r"'m'"):
        deep_merge_into_node(target, {"m": "scalar"})


def test_deep_merge_list_over_scalar_raises_type_mismatch() -> None:
    src = _yload("root:\n  s: 1\n")
    target = copy.deepcopy(_ynode_at(src, "root"))

    with pytest.raises(MergeTypeMismatch, match=r"'s'"):
        deep_merge_into_node(target, {"s": [1, 2, 3]})
