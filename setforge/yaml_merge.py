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
from enum import StrEnum
from typing import Any

from setforge.errors import MergeTypeMismatch

_TOKEN_RE = re.compile(r"^(?P<key>[^.\[\]]+)(?P<suffix>\[\*\]|\[\])?$")


class NodeShape(StrEnum):
    """The three structural shapes a YAML node can take.

    Members are :class:`str`, so f-string interpolation and ``str()`` yield the
    bare ``.value`` (``"dict"``/``"list"``/``"scalar"``) used in user-facing
    :class:`~setforge.errors.MergeTypeMismatch` message text.
    """

    DICT = "dict"
    LIST = "list"
    SCALAR = "scalar"


class PathTokenKind(StrEnum):
    """The kind of a parsed path token.

    :attr:`KEY` is a plain dict key, :attr:`KEY_EACH` a final ``[*]`` (per-element
    overlay), :attr:`KEY_WHOLE` a final ``[]`` (whole-list replace).
    """

    KEY = "key"
    KEY_EACH = "key_each"
    KEY_WHOLE = "key_whole"


def _parse_path(path: str) -> list[tuple[PathTokenKind, str]]:
    """Return a list of ``(kind, key)`` tuples.

    ``kind`` is :attr:`PathTokenKind.KEY` for a plain dict key,
    :attr:`PathTokenKind.KEY_EACH` for a final ``[*]`` segment, or
    :attr:`PathTokenKind.KEY_WHOLE` for a final ``[]`` segment.
    """
    parts = path.split(".")
    if not parts or any(not p for p in parts):
        raise ValueError(f"invalid path: {path!r}")
    tokens: list[tuple[PathTokenKind, str]] = []
    last = len(parts) - 1
    for i, part in enumerate(parts):
        match = _TOKEN_RE.match(part)
        if not match:
            raise ValueError(f"invalid path token {part!r} in {path!r}")
        key = match.group("key")
        suffix = match.group("suffix")
        if suffix is None:
            tokens.append((PathTokenKind.KEY, key))
        elif i != last:
            raise ValueError(
                f"list suffix {suffix!r} only allowed at end of path: {path!r}"
            )
        elif suffix == "[*]":
            tokens.append((PathTokenKind.KEY_EACH, key))
        else:
            tokens.append((PathTokenKind.KEY_WHOLE, key))
    return tokens


def _shape(value: Any) -> NodeShape:
    if isinstance(value, Mapping):
        return NodeShape.DICT
    if isinstance(value, list):
        return NodeShape.LIST
    return NodeShape.SCALAR


def _check_leaf_type(src_val: Any, live_val: Any, path: str) -> None:
    if _shape(src_val) != _shape(live_val):
        raise MergeTypeMismatch(
            f"type mismatch at {path!r}: src is {_shape(src_val)}, "
            f"live is {_shape(live_val)}"
        )


def overlay(
    src_doc: Any,
    live_doc: Any,
    key_paths: list[str],
    deep_key_paths: list[str] | None = None,
) -> Any:
    """Return a deep copy of ``src_doc`` with ``live_doc``'s values overlaid
    at every JSONPath-lite path in ``key_paths``.

    Conflict rules (shallow mode, ``key_paths``):

    - Path absent in ``live_doc`` → keep src's value.
    - Path absent in ``src_doc`` → add live's key (whole subtree).
    - Leaf type mismatch (str vs list, scalar vs dict, etc.) at a preserved
      path → raise :class:`MergeTypeMismatch`.

    Deep mode (``deep_key_paths``):

    The terminal value at each path is recursively deep-merged: tracked-only
    sub-keys survive, live-only sub-keys are added, shared scalars take live's
    value, shared dicts recurse, shared lists are whole-replaced (live wins).
    Type mismatches at any depth raise :class:`MergeTypeMismatch`. The deep
    loop runs after the shallow loop so deep-merge sees src post-shallow.
    """
    result = copy.deepcopy(src_doc)
    for path in key_paths:
        tokens = _parse_path(path)
        _apply_overlay(result, live_doc, tokens, path)
    for path in deep_key_paths or []:
        tokens = _parse_path(path)
        _apply_deep_overlay(result, live_doc, tokens, path)
    return result


def _apply_overlay(
    src_node: Any,
    live_node: Any,
    tokens: list[tuple[PathTokenKind, str]],
    path: str,
) -> None:
    kind, key = tokens[0]
    rest = tokens[1:]

    if not isinstance(live_node, Mapping) or key not in live_node:
        return
    live_value = live_node[key]

    if not isinstance(src_node, MutableMapping):
        raise MergeTypeMismatch(f"cannot descend into non-mapping at {path!r}")

    if kind == PathTokenKind.KEY:
        _overlay_scalar(src_node, key, live_value, rest, path)
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

    if kind == PathTokenKind.KEY_WHOLE:
        _overlay_list_whole(src_node[key], live_value)
        return
    if kind == PathTokenKind.KEY_EACH:
        _overlay_list_each(src_node[key], live_value)


def _overlay_scalar(
    src_node: MutableMapping,
    key: str,
    live_value: Any,
    rest: list[tuple[PathTokenKind, str]],
    path: str,
) -> None:
    """Handle a plain ``key`` token at the head of the remaining path.

    Terminal step → leaf-type-check and replace; non-terminal step →
    copy live's subtree when src lacks the key, otherwise recurse.
    """
    if not rest:
        if key in src_node:
            _check_leaf_type(src_node[key], live_value, path)
        src_node[key] = copy.deepcopy(live_value)
        return
    if key not in src_node:
        src_node[key] = copy.deepcopy(live_value)
        return
    _apply_overlay(src_node[key], live_value, rest, path)


def _overlay_list_whole(src_list: list[Any], live_value: list[Any]) -> None:
    """Whole-list replace (``[]`` suffix): drop src, copy every live element."""
    src_list.clear()
    src_list.extend(copy.deepcopy(item) for item in live_value)


def _overlay_list_each(src_list: list[Any], live_value: list[Any]) -> None:
    """Per-element overlay (``[*]`` suffix): overwrite shared indices, append tail."""
    for i in range(min(len(src_list), len(live_value))):
        src_list[i] = copy.deepcopy(live_value[i])
    for i in range(len(src_list), len(live_value)):
        src_list.append(copy.deepcopy(live_value[i]))


def _apply_deep_overlay(
    src_node: Any,
    live_node: Any,
    tokens: list[tuple[PathTokenKind, str]],
    path: str,
) -> None:
    """Like :func:`_apply_overlay` but at the terminal, deep-merge dicts
    instead of whole-leaf replace. The two helpers mirror each other's
    intermediate-walk shape; they diverge only on the terminal step.
    """
    kind, key = tokens[0]
    rest = tokens[1:]
    if kind != PathTokenKind.KEY:
        # Validator on TrackedFile.preserve_user_keys_deep already rejects
        # [*] / [] suffixes — defensive only.
        raise ValueError(f"deep overlay does not support {path!r}")
    if not isinstance(live_node, Mapping) or key not in live_node:
        return
    live_value = live_node[key]
    if not isinstance(src_node, MutableMapping):
        raise MergeTypeMismatch(f"cannot descend into non-mapping at {path!r}")

    if not rest:
        # Terminal: deep-merge the dict at src_node[key] with live_value.
        if key not in src_node:
            src_node[key] = copy.deepcopy(live_value)
            return
        src_value = src_node[key]
        if not (
            isinstance(src_value, MutableMapping) and isinstance(live_value, Mapping)
        ):
            raise MergeTypeMismatch(
                f"deep-merge at {path!r} requires dict on both sides; "
                f"got src={_shape(src_value)}, live={_shape(live_value)}"
            )
        _deep_merge_dicts(src_value, live_value, path)
        return

    # Non-terminal: walk down (mirror _apply_overlay's intermediate case).
    if key not in src_node:
        src_node[key] = copy.deepcopy(live_value)
        return
    _apply_deep_overlay(src_node[key], live_value, rest, path)


def _deep_merge_dicts(
    src_dict: MutableMapping,
    live_dict: Mapping,
    path: str,
) -> None:
    """Mutate ``src_dict`` in place: union of keys, live wins on shared
    scalars, recurse on shared dicts, raise on shape mismatch, whole-list
    replace on shared arrays. Live-only keys added; tracked-only kept.
    """
    for key, live_value in live_dict.items():
        sub_path = f"{path}.{key}"
        if key not in src_dict:
            src_dict[key] = copy.deepcopy(live_value)
            continue
        src_value = src_dict[key]
        match (src_value, live_value):
            case (MutableMapping(), Mapping()):
                _deep_merge_dicts(src_value, live_value, sub_path)
            case (list(), list()):
                src_value.clear()
                src_value.extend(copy.deepcopy(item) for item in live_value)
            case _ if _shape(src_value) != _shape(live_value):
                raise MergeTypeMismatch(
                    f"type mismatch at {sub_path!r}: src is {_shape(src_value)}, "
                    f"live is {_shape(live_value)}"
                )
            case _:
                # Both scalars: live wins.
                src_dict[key] = copy.deepcopy(live_value)


_MISSING = object()


def _navigate(node: Any, tokens: list[tuple[PathTokenKind, str]]) -> Any:
    if not tokens:
        return node
    kind, key = tokens[0]
    rest = tokens[1:]
    if not isinstance(node, Mapping) or key not in node:
        return _MISSING
    if kind == PathTokenKind.KEY:
        return _navigate(node[key], rest)
    return node[key]
