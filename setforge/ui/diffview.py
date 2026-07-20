"""Shared themed diff renderer — one model, two render surfaces (A6, A7)."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from enum import StrEnum, auto
from functools import cache

from rich.console import Group, RenderableType
from rich.style import Style
from rich.table import Table
from rich.text import Text

from setforge.reconcile import Clean, MergeResult
from setforge.ui.text import sanitize_controls
from setforge.ui.theme import THEME, Role

_MAX_BYTES = 5 * 1024 * 1024

_OURS_MARKER = "<<<<<<< OURS (this host)"
_SEP_MARKER = "======="
_THEIRS_MARKER = ">>>>>>> THEIRS (upstream)"


class RowKind(StrEnum):
    CTX = "ctx"
    ADD = "add"
    DEL = "del"
    CONFLICT = "conflict"


class Side(StrEnum):
    LIVE = "live"
    UPSTREAM = "upstream"
    BOTH = "both"


class RichLayout(StrEnum):
    STACKED = auto()
    SIDE_BY_SIDE = auto()


_KIND_ROLE: dict[RowKind, Role] = {
    RowKind.CTX: Role.MUTED,
    RowKind.ADD: Role.WARNING,
    RowKind.DEL: Role.SUCCESS,
    RowKind.CONFLICT: Role.MUTED,
}


@dataclass(frozen=True, slots=True)
class DiffRow:
    kind: RowKind
    text: str
    side: Side


@dataclass(frozen=True, slots=True)
class DiffModel:
    rows: list[DiffRow] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    hunks: int = 0
    binary: bool = False

    @property
    def summary(self) -> str:
        unit = "hunk" if self.hunks == 1 else "hunks"
        return f"+{self.added} -{self.removed} · {self.hunks} {unit}"


def _is_binary(*blobs: bytes) -> bool:
    return any(b"\x00" in blob or len(blob) > _MAX_BYTES for blob in blobs)


def _text(data: bytes) -> str:
    return sanitize_controls(data.decode("utf-8", errors="replace"))


def _stat_model(*blobs: bytes) -> DiffModel:
    total = sum(len(blob) for blob in blobs)
    row = DiffRow(
        kind=RowKind.CTX,
        text=f"binary — {total} bytes, cannot diff",
        side=Side.BOTH,
    )
    return DiffModel(rows=[row], binary=True)


def two_way_lines(
    live: bytes, upstream: bytes, *, context: int | None = None
) -> DiffModel:
    if _is_binary(live, upstream):
        return _stat_model(live, upstream)

    a = _text(live).splitlines(keepends=True)
    b = _text(upstream).splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)

    n = max(len(a), len(b)) if context is None else context

    rows: list[DiffRow] = []
    added = removed = hunks = 0
    for group in matcher.get_grouped_opcodes(n):
        hunks += 1
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows.extend(DiffRow(RowKind.CTX, line, Side.BOTH) for line in a[i1:i2])
                continue
            if tag in ("replace", "delete"):
                removed += i2 - i1
                rows.extend(DiffRow(RowKind.DEL, line, Side.LIVE) for line in a[i1:i2])
            if tag in ("replace", "insert"):
                added += j2 - j1
                rows.extend(
                    DiffRow(RowKind.ADD, line, Side.UPSTREAM) for line in b[j1:j2]
                )

    # get_grouped_opcodes yields nothing for an identical pair.
    if not rows and a == b:
        rows = [DiffRow(RowKind.CTX, line, Side.BOTH) for line in a]
    return DiffModel(rows=rows, added=added, removed=removed, hunks=hunks)


def three_way_segments(result: MergeResult) -> DiffModel:
    raw = [
        seg.bytes_ if isinstance(seg, Clean) else seg.ours + seg.theirs + seg.base
        for seg in result.segments
    ]
    if _is_binary(*raw):
        return _stat_model(*raw)

    rows: list[DiffRow] = []
    added = removed = hunks = 0
    for seg in result.segments:
        if isinstance(seg, Clean):
            rows.extend(
                DiffRow(RowKind.CTX, line, Side.BOTH)
                for line in _text(seg.bytes_).splitlines(keepends=True)
            )
            continue
        hunks += 1
        rows.append(DiffRow(RowKind.CONFLICT, f"{_OURS_MARKER}\n", Side.BOTH))
        for line in _text(seg.ours).splitlines(keepends=True):
            removed += 1
            rows.append(DiffRow(RowKind.DEL, line, Side.LIVE))
        rows.append(DiffRow(RowKind.CONFLICT, f"{_SEP_MARKER}\n", Side.BOTH))
        for line in _text(seg.theirs).splitlines(keepends=True):
            added += 1
            rows.append(DiffRow(RowKind.ADD, line, Side.UPSTREAM))
        rows.append(DiffRow(RowKind.CONFLICT, f"{_THEIRS_MARKER}\n", Side.BOTH))
    return DiffModel(rows=rows, added=added, removed=removed, hunks=hunks)


def _sigil(kind: RowKind) -> str:
    return {RowKind.ADD: "+", RowKind.DEL: "-", RowKind.CONFLICT: "!"}.get(kind, " ")


def to_fragments(m: DiffModel, cap: int | None = None) -> list[tuple[str, str]]:
    rows = m.rows if cap is None else m.rows[:cap]
    frags: list[tuple[str, str]] = []
    for row in rows:
        role = _KIND_ROLE[row.kind]
        text = row.text if row.text.endswith("\n") else row.text + "\n"
        prefix = "" if row.kind is RowKind.CONFLICT else _sigil(row.kind)
        frags.append((f"class:{role.value}", f"{prefix}{text}"))
    return frags


@cache
def _role_style(role: Role) -> Style:
    # rich no-ops on prompt_toolkit's "class:" token; needs a real truecolor Style.
    return Style.parse(THEME[role].truecolor)


def _row_text(row: DiffRow) -> Text:
    role = _KIND_ROLE[row.kind]
    body = row.text.rstrip("\n")
    prefix = "" if row.kind is RowKind.CONFLICT else _sigil(row.kind)
    return Text(f"{prefix}{body}", style=_role_style(role))


def _side_by_side_rows(rows: list[DiffRow]) -> list[tuple[Text, Text]]:
    """Pair rows into side-by-side ``(left, right)`` cells preserving hunk locality.

    Walks the row stream in order rather than filtering by :class:`Side` and
    zipping by raw index (which cross-pairs a deletion with an unrelated
    addition from another hunk). Context lines show identically in both
    columns; a conflict marker spans the left column (empty right); a ``DEL``
    run and the ``ADD`` run that immediately follows it (a replace hunk) are
    zipped row-for-row with blank padding on the shorter side, so a deletion
    aligns against its actual replacement.
    """
    cells: list[tuple[Text, Text]] = []
    i, n = 0, len(rows)
    while i < n:
        row = rows[i]
        if row.kind is RowKind.CTX:
            shared = _row_text(row)
            cells.append((shared, shared))
            i += 1
        elif row.kind is RowKind.CONFLICT:
            cells.append((_row_text(row), Text()))
            i += 1
        else:
            dels: list[DiffRow] = []
            while i < n and rows[i].kind is RowKind.DEL:
                dels.append(rows[i])
                i += 1
            adds: list[DiffRow] = []
            while i < n and rows[i].kind is RowKind.ADD:
                adds.append(rows[i])
                i += 1
            for k in range(max(len(dels), len(adds))):
                lcell = _row_text(dels[k]) if k < len(dels) else Text()
                rcell = _row_text(adds[k]) if k < len(adds) else Text()
                cells.append((lcell, rcell))
    return cells


def to_rich(m: DiffModel, *, layout: RichLayout) -> RenderableType:
    if layout is RichLayout.SIDE_BY_SIDE:
        table = Table.grid(expand=True)
        # overflow="fold" hard-wraps a long unbroken token (path / URL / plugin
        # id) across physical rows inside the half-width cell instead of the
        # grid default of ellipsis-cropping its tail — losing the tail would
        # hide a real byte difference in the exact view used for a merge call.
        table.add_column(ratio=1, overflow="fold")
        table.add_column(ratio=1, overflow="fold")
        for lcell, rcell in _side_by_side_rows(m.rows):
            table.add_row(lcell, rcell)
        return table

    return Group(*(_row_text(row) for row in m.rows))
