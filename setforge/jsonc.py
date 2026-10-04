"""json-five helpers shared by the structured merge and staging code.

``.json`` files are parsed as JSONC through :mod:`json5` (PyPI ``json-five``),
whose ``ModelLoader`` / ``ModelDumper`` pair keeps comments and formatting.
"""

from pathlib import Path
from typing import Any

from json5.model import (
    DoubleQuotedString,
    Identifier,
    JSONObject,
    SingleQuotedString,
)


def is_jsonc_file(path: Path) -> bool:
    """True iff ``path`` should be treated as JSONC.

    Heuristic by extension: every ``.json`` file in this codebase is
    JSONC-capable (matches VSCode's behavior — ``.json`` is parsed as
    JSONC there). Tracked Claude ``settings.json`` is plain JSON in
    practice but still parses cleanly through the JSONC pipeline; no
    need for a content sniff.
    """
    return path.suffix == ".json"


def _find_key_index(top_obj: JSONObject, name: str) -> int | None:
    for i, key in enumerate(top_obj.keys):
        if _key_text(key) == name:
            return i
    return None


def _key_text(key_node: Any) -> str:
    """Return the key's literal characters regardless of quote style.

    JSON5 supports unquoted identifier keys and single-quoted strings in
    addition to JSONC's double-quoted; ``DoubleQuotedString`` /
    ``SingleQuotedString`` / ``Identifier`` all expose ``.characters``
    (or ``.name`` for identifiers) — but we only ever generate
    double-quoted, and VSCode emits double-quoted. For input we read,
    fall back to a string repr if the node shape is unexpected.
    """
    match key_node:
        case DoubleQuotedString(characters=c) | SingleQuotedString(characters=c):
            return str(c)
        case Identifier(name=n):
            return str(n)
        case _:
            return str(key_node)
