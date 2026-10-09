"""An edit to a hand-styled YAML file reads back as intended and keeps the rest.

Documents are generated with what people really write: every list and mapping
indented its own way, blank lines and comments in every position, CRLF, a BOM,
no final newline. One ``config add`` / ``config remove`` style edit is applied
and the rendered text must parse to exactly the edited data, keep every comment
and blank line outside the removed entry exactly once and in its original order,
and leave every line the edit did not own byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from setforge.cli._config_helpers import apply_add, apply_remove
from setforge.migrations._yaml_ops import render_yaml, yaml_rt

_Path = tuple[str, ...]


@dataclass
class _Entry:
    """Where one key (or list item) sits in the generated text."""

    first: int
    last: int = -1
    kind: str = "scalar"
    children: list[_Path] = field(default_factory=list)
    item_lines: list[int] = field(default_factory=list)


@dataclass
class _Doc:
    lines: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    entries: dict[_Path, _Entry] = field(default_factory=dict)
    top: list[_Path] = field(default_factory=list)
    names: int = 0

    def name(self, prefix: str) -> str:
        self.names += 1
        return f"{prefix}{self.names}"


def _noise(draw: st.DrawFn, doc: _Doc, col: int) -> None:
    """Blank and comment lines before an entry, at its column or at none."""
    for kind in draw(st.lists(st.sampled_from(["blank", "comment"]), max_size=2)):
        if kind == "blank":
            doc.lines.append("")
        else:
            doc.lines.append(" " * col + f"# {doc.name('c')}.")


def _eol(draw: st.DrawFn, doc: _Doc) -> str:
    if not draw(st.booleans()):
        return ""
    return " " * draw(st.integers(1, 3)) + f"# {doc.name('c')}."


def _quoted(draw: st.DrawFn, value: str) -> str:
    quote = draw(st.sampled_from(["", "'", '"']))
    return f"{quote}{value}{quote}"


def _mapping(
    draw: st.DrawFn, doc: _Doc, col: int, path: _Path, depth: int
) -> tuple[dict[str, Any], list[_Path]]:
    data: dict[str, Any] = {}
    order: list[_Path] = []
    kinds = ["scalar", "list", "maplist", "flow"] + (["map"] if depth < 3 else [])
    for kind in draw(st.lists(st.sampled_from(kinds), min_size=1, max_size=3)):
        key = doc.name("k")
        here = (*path, key)
        order.append(here)
        _noise(draw, doc, col)
        entry = _Entry(first=len(doc.lines), kind=kind)
        doc.entries[here] = entry
        pad = " " * col
        if kind == "scalar":
            value = doc.name("v")
            doc.lines.append(f"{pad}{key}: {_quoted(draw, value)}{_eol(draw, doc)}")
            data[key] = value
        elif kind == "flow":
            items = [doc.name("v") for _ in range(draw(st.integers(1, 3)))]
            doc.lines.append(f"{pad}{key}: [{', '.join(items)}]{_eol(draw, doc)}")
            data[key] = items
        elif kind == "list":
            doc.lines.append(f"{pad}{key}:{_eol(draw, doc)}")
            dash = col + draw(st.sampled_from([0, 2, 3, 4]))
            items = []
            for _ in range(draw(st.integers(1, 3))):
                _noise(draw, doc, dash)
                value = doc.name("v")
                entry.item_lines.append(len(doc.lines))
                quoted = _quoted(draw, value)
                doc.lines.append(f"{' ' * dash}- {quoted}{_eol(draw, doc)}")
                items.append(value)
            data[key] = items
        elif kind == "maplist":
            doc.lines.append(f"{pad}{key}:{_eol(draw, doc)}")
            dash = col + draw(st.sampled_from([0, 2, 3, 4]))
            records = []
            for _ in range(draw(st.integers(1, 2))):
                _noise(draw, doc, dash)
                record = {}
                for lead in ["- ", "  "][: draw(st.integers(1, 2))]:
                    name, value = doc.name("k"), doc.name("v")
                    line = f"{' ' * dash}{lead}{name}: {value}{_eol(draw, doc)}"
                    doc.lines.append(line)
                    record[name] = value
                records.append(record)
            data[key] = records
        else:
            doc.lines.append(f"{pad}{key}:{_eol(draw, doc)}")
            step = draw(st.sampled_from([2, 3, 4]))
            data[key], entry.children = _mapping(draw, doc, col + step, here, depth + 1)
        entry.last = len(doc.lines) - 1
    return data, order


@st.composite
def _documents(draw: st.DrawFn) -> _Doc:
    doc = _Doc()
    if draw(st.integers(0, 9)) == 0:
        # A file of only comments: the first edit must keep them.
        for _ in range(draw(st.integers(1, 3))):
            doc.lines.append(f"# {doc.name('c')}.")
        return doc
    doc.data, doc.top = _mapping(draw, doc, 0, (), 1)
    _noise(draw, doc, 0)
    return doc


def _siblings(doc: _Doc, path: _Path) -> list[_Path]:
    return doc.entries[path[:-1]].children if len(path) > 1 else doc.top


def _at(data: dict[str, Any], path: _Path) -> Any:
    node: Any = data
    for part in path:
        node = node[part]
    return node


@st.composite
def _edits(draw: st.DrawFn) -> tuple[_Doc, CommentedMap, range, list[int]]:
    """A document with one edit applied to its round-trip tree.

    Returns ``(doc, edited tree, removed, owned)``: ``removed`` are the lines
    of the entry the edit took out (its comments go with it) and ``owned`` the
    lines the edit may rewrite: those, and the line of a key whose value the
    edit emptied (it becomes ``key: {}`` or ``key: []``).
    """
    doc = draw(_documents())
    by_kind: dict[str, list[_Path]] = {
        kind: [] for kind in ("scalar", "list", "maplist", "flow", "map")
    }
    for path, entry in doc.entries.items():
        by_kind[entry.kind].append(path)
    ops = ["add_key", "add_list"]
    ops += ["unset"] if doc.entries else []
    ops += ["set"] if by_kind["scalar"] else []
    ops += ["append", "remove_item"] if by_kind["list"] else []
    op = draw(st.sampled_from(ops))
    tree = yaml_rt().load("\n".join(doc.lines) + "\n") or CommentedMap()
    removed: range = range(0)
    owned: list[int] = []
    if op in ("add_key", "add_list"):
        parent = draw(st.sampled_from([(), *by_kind["map"]]))
        extra = draw(st.sampled_from([(), ("n1",), ("n1", "n2")]))
        dotted = ".".join((*parent, *extra, "added"))
        apply_add(tree, dotted, "fresh", is_list=op == "add_list")
    elif op == "set":
        path = draw(st.sampled_from(by_kind["scalar"]))
        apply_add(tree, ".".join(path), "fresh", is_list=False)
        owned = [doc.entries[path].first]
    elif op == "append":
        path = draw(st.sampled_from(by_kind["list"]))
        apply_add(tree, ".".join(path), "fresh", is_list=True)
    elif op == "remove_item":
        path = draw(st.sampled_from(by_kind["list"]))
        entry = doc.entries[path]
        index = draw(st.integers(0, len(entry.item_lines) - 1))
        value = _at(doc.data, path)[index]
        apply_remove(tree, ".".join(path), value, is_list=True)
        line = entry.item_lines[index]
        removed = range(line, line + 1)
        emptied = [entry.first] if len(entry.item_lines) == 1 else []
        owned = [*removed, *emptied]
    else:
        path = draw(st.sampled_from(list(doc.entries)))
        apply_remove(tree, ".".join(path), None, is_list=False)
        removed = range(doc.entries[path].first, doc.entries[path].last + 1)
        only = len(_siblings(doc, path)) == 1 and len(path) > 1
        owned = [*removed, *([doc.entries[path[:-1]].first] if only else [])]
    return doc, tree, removed, owned


def _notes(lines: list[str]) -> list[str]:
    """Every comment and blank line of ``lines``, in order."""
    notes: list[str] = []
    for line in lines:
        notes += (
            [f"# {part}" for part in line.split("# ")[1:]] if line.strip() else [""]
        )
    return notes


def _plain(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _plain(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_plain(value) for value in node]
    return str(node) if isinstance(node, str) else node


_FRAMINGS = st.tuples(
    st.sampled_from(["\n", "\r\n"]), st.booleans(), st.sampled_from(["", "﻿"])
)


@pytest.mark.slow  # about 2.5s: 400 generated documents, each rendered and parsed
@settings(max_examples=400)
@given(case=_edits(), framing=_FRAMINGS)
def test_an_edit_reads_back_and_keeps_every_line_it_does_not_own(
    case: tuple[_Doc, CommentedMap, range, list[int]],
    framing: tuple[str, bool, str],
) -> None:
    doc, tree, removed, owned = case
    eol, final_newline, bom = framing
    # A one-line file with no final newline has no line ending to carry over.
    assume(final_newline or len(doc.lines) > 1)
    # The tree was parsed from the lines plus a final newline: a trailing blank
    # line would be in it but not in an original that has none.
    assume(final_newline or doc.lines[-1])
    original = bom + eol.join(doc.lines) + (eol if final_newline else "")

    rendered = render_yaml(tree, original)

    assert rendered.startswith(bom)
    body = rendered.removeprefix(bom)
    assert body.endswith(eol)
    assert body.count("\n") == body.count(eol)
    assert YAML(typ="safe").load(body) == _plain(tree)
    out_lines = body.removesuffix(eol).split(eol)
    for number, line in enumerate(doc.lines):
        if number in removed:
            continue
        for part in line.split("# ")[1:]:
            assert sum(f"# {part}" in out for out in out_lines) == 1, part
    left = [line for number, line in enumerate(doc.lines) if number not in removed]
    assert _notes(out_lines) == _notes(left)
    remaining = iter(out_lines)
    for number, line in enumerate(doc.lines):
        if number not in owned:
            assert line in remaining, (number, line)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "k:\n  - {x: 1}\n  # about b\n  - b\n  - c\n",
            "k:\n  - {x: 1}\n  # about b\n  - c\n",
            id="between-two-entries",
        ),
        pytest.param(
            "k:\n  - [a]\n  # about b\n  - b\n# after\nz: 1\n",
            "k:\n  - [a]\n  # about b\n# after\nz: 1\n",
            id="last-entry",
        ),
        pytest.param(
            "k:  # mine\n  - {x: 1, y: 2}\n  # about b\n  - b  # gone\n"
            "  - p: 1\n    q: 2\n",
            "k:  # mine\n  - {x: 1, y: 2}\n  # about b\n  - p: 1\n    q: 2\n",
            id="before-a-mapping-entry-under-a-commented-key",
        ),
    ],
)
def test_removing_an_entry_below_a_one_line_entry_keeps_the_comment_above_it(
    text: str, expected: str
) -> None:
    """No list ``config remove`` edits may hold ``- {x: 1}``, so this is direct.

    The comment used to be deleted with the entry.
    """
    tree = yaml_rt().load(text)

    apply_remove(tree, "k", "b", is_list=True)

    assert render_yaml(tree, text) == expected
