"""YAML overlay and key extraction helpers (pure, no I/O).

Path syntax (locked in the rewrite plan):

- ``a.b.c``  → ``doc['a']['b']['c']`` (dotted dict descent)
- ``a.b[*]`` → every list element under ``doc['a']['b']`` (per-element overlay)
- ``a.b[]``  → the entire list at ``doc['a']['b']`` (whole-list replace)

``[*]`` and ``[]`` may appear only at the end of a path.
"""

import copy
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

from my_setup.errors import MergeTypeMismatch

_TOKEN_RE = re.compile(r"^(?P<key>[^.\[\]]+)(?P<suffix>\[\*\]|\[\])?$")


def _parse_path(path: str) -> list[tuple[str, str]]:
    """Return a list of ``(kind, key)`` tuples.

    ``kind`` is ``"key"`` for a plain dict key, ``"key_each"`` for a final
    ``[*]`` segment, or ``"key_whole"`` for a final ``[]`` segment.
    """
    parts = path.split(".")
    if not parts or any(not p for p in parts):
        raise ValueError(f"invalid path: {path!r}")
    tokens: list[tuple[str, str]] = []
    last = len(parts) - 1
    for i, part in enumerate(parts):
        match = _TOKEN_RE.match(part)
        if not match:
            raise ValueError(f"invalid path token {part!r} in {path!r}")
        key = match.group("key")
        suffix = match.group("suffix")
        if suffix is None:
            tokens.append(("key", key))
        elif i != last:
            raise ValueError(
                f"list suffix {suffix!r} only allowed at end of path: {path!r}"
            )
        elif suffix == "[*]":
            tokens.append(("key_each", key))
        else:
            tokens.append(("key_whole", key))
    return tokens


def _shape(value: Any) -> str:
    if isinstance(value, Mapping):
        return "dict"
    if isinstance(value, list):
        return "list"
    return "scalar"


def _check_leaf_type(src_val: Any, live_val: Any, path: str) -> None:
    if _shape(src_val) != _shape(live_val):
        raise MergeTypeMismatch(
            f"type mismatch at {path!r}: src is {_shape(src_val)}, "
            f"live is {_shape(live_val)}"
        )


def overlay(src_doc: Any, live_doc: Any, key_paths: list[str]) -> Any:
    """Return a deep copy of ``src_doc`` with ``live_doc``'s values overlaid
    at every JSONPath-lite path in ``key_paths``.

    Conflict rules:

    - Path absent in ``live_doc`` → keep src's value.
    - Path absent in ``src_doc`` → add live's key (whole subtree).
    - Leaf type mismatch (str vs list, scalar vs dict, etc.) at a preserved
      path → raise :class:`MergeTypeMismatch`.
    """
    result = copy.deepcopy(src_doc)
    for path in key_paths:
        tokens = _parse_path(path)
        _apply_overlay(result, live_doc, tokens, path)
    return result


def _apply_overlay(
    src_node: Any,
    live_node: Any,
    tokens: list[tuple[str, str]],
    path: str,
) -> None:
    kind, key = tokens[0]
    rest = tokens[1:]

    if not isinstance(live_node, Mapping) or key not in live_node:
        return
    live_value = live_node[key]

    if not isinstance(src_node, MutableMapping):
        raise MergeTypeMismatch(
            f"cannot descend into non-mapping at {path!r}"
        )

    if kind == "key":
        if not rest:
            if key in src_node:
                _check_leaf_type(src_node[key], live_value, path)
            src_node[key] = copy.deepcopy(live_value)
            return
        if key not in src_node:
            src_node[key] = copy.deepcopy(live_value)
            return
        _apply_overlay(src_node[key], live_value, rest, path)
        return

    if not isinstance(live_value, list):
        return

    if key not in src_node:
        src_node[key] = copy.deepcopy(live_value)
        return
    if not isinstance(src_node[key], list):
        raise MergeTypeMismatch(
            f"type mismatch at {path!r}: src is {_shape(src_node[key])}, live is list"
        )

    if kind == "key_whole":
        src_list = src_node[key]
        src_list.clear()
        src_list.extend(copy.deepcopy(item) for item in live_value)
        return

    if kind == "key_each":
        src_list = src_node[key]
        for i in range(min(len(src_list), len(live_value))):
            src_list[i] = copy.deepcopy(live_value[i])
        for i in range(len(src_list), len(live_value)):
            src_list.append(copy.deepcopy(live_value[i]))
        return


def extract_keys(doc: Any, key_paths: list[str]) -> dict[str, Any]:
    """Return a flat ``{path: value}`` dict of values at each path in ``doc``.

    Missing paths are silently skipped. Used by :mod:`my_setup.capture` to
    know which user-key values to strip from live before writing tracked,
    and by :mod:`my_setup.compare` to render an apples-to-apples view for
    drift classification.
    """
    result: dict[str, Any] = {}
    for path in key_paths:
        tokens = _parse_path(path)
        value = _navigate(doc, tokens)
        if value is _MISSING:
            continue
        result[path] = value
    return result


_MISSING = object()


def _navigate(node: Any, tokens: list[tuple[str, str]]) -> Any:
    if not tokens:
        return node
    kind, key = tokens[0]
    rest = tokens[1:]
    if not isinstance(node, Mapping) or key not in node:
        return _MISSING
    if kind == "key":
        return _navigate(node[key], rest)
    return node[key]


def delete_keys(doc: Any, key_paths: list[str]) -> None:
    """Mutate ``doc`` in place, removing the value at every path in
    ``key_paths``. Missing paths are silently skipped.

    For ``[*]`` and ``[]`` paths the entire list at the path is removed
    (per-element delete is meaningless for capture's strip use case).
    """
    for path in key_paths:
        tokens = _parse_path(path)
        _delete_path(doc, tokens)


def _delete_path(node: Any, tokens: list[tuple[str, str]]) -> None:
    if not tokens:
        return
    kind, key = tokens[0]
    rest = tokens[1:]
    if not isinstance(node, MutableMapping) or key not in node:
        return
    if not rest:
        del node[key]
        return
    _delete_path(node[key], rest)
