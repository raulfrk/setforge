"""YAML path parsing and navigation helpers (pure, no I/O).

Path syntax (locked in the rewrite plan):

- ``a.b.c``  → ``doc['a']['b']['c']`` (dotted dict descent)
- ``a.b[*]`` → every list element under ``doc['a']['b']`` (per-element access)
- ``a.b[]``  → the entire list at ``doc['a']['b']`` (whole-list access)

``[*]`` and ``[]`` may appear only at the end of a path.
"""

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

_TOKEN_RE = re.compile(r"^(?P<key>[^.\[\]]+)(?P<suffix>\[\*\]|\[\])?$")


class PathTokenKind(StrEnum):
    """The kind of a parsed path token.

    :attr:`KEY` is a plain dict key, :attr:`KEY_EACH` a final ``[*]``
    (per-element), :attr:`KEY_WHOLE` a final ``[]`` (whole-list).
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
