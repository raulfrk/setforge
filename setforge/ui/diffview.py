"""Shared themed diff renderer — one model, two render surfaces (A6, A7)."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from enum import StrEnum, auto

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.text import Text

from setforge.reconcile import Clean, MergeResult
from setforge.ui.text import sanitize_controls
from setforge.ui.theme import Role

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


def _row_text(row: DiffRow) -> Text:
    role = _KIND_ROLE[row.kind]
    body = row.text.rstrip("\n")
    prefix = "" if row.kind is RowKind.CONFLICT else _sigil(row.kind)
    return Text(f"{prefix}{body}", style=f"class:{role.value}")


def to_rich(m: DiffModel, *, layout: RichLayout) -> RenderableType:
    if layout is RichLayout.SIDE_BY_SIDE:
        left = Text()
        right = Text()
        for row in m.rows:
            target = right if row.side is Side.UPSTREAM else left
            target.append_text(_row_text(row))
            target.append("\n")
        return Columns([left, right], equal=True, expand=True)

    return Group(*(_row_text(row) for row in m.rows))
