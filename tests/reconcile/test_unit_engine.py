"""The per-format staging engine and the unit hash name both kinds share."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from setforge.errors import InvariantViolation
from setforge.reconcile.hunks import extract_hunks
from setforge.reconcile.index_model import require_unit_kind
from setforge.reconcile.structured_units import (
    StructuredFormat,
    extract_structured_units,
)
from setforge.reconcile.types import HunkClass, UnitKind
from setforge.reconcile.unit_engine import (
    LINE,
    UnitEngine,
    engine_for,
    structured_engine,
)

_PARSES = b"a: 1\n"
_BROKEN = b"a: {broken\n"
_LINE_ROW: dict[str, object] = {
    "kind": "line",
    "unit_id": "u",
    "live_hash": "h",
    "cls": "local",
}
_KEY_ROW: dict[str, object] = {
    "kind": "key",
    "path": "a",
    "value_hash": "h",
    "cls": "local",
}


@pytest.mark.parametrize(
    ("name", "base", "kind", "fmt"),
    [
        ("notes.md", _PARSES, UnitKind.LINE, None),
        ("notes.md", _BROKEN, UnitKind.LINE, None),
        ("settings.jsonc", b'{"a": 1}', UnitKind.LINE, None),
        ("settings.yaml", _PARSES, UnitKind.KEY, StructuredFormat.YAML),
        ("settings.yml", _PARSES, UnitKind.KEY, StructuredFormat.YAML),
        ("settings.json", b'{"a": 1}', UnitKind.KEY, StructuredFormat.JSONC),
        ("settings.yaml", _BROKEN, UnitKind.LINE, None),
        ("settings.yml", _BROKEN, UnitKind.LINE, None),
        ("settings.yaml", b"\xff\xfe", UnitKind.LINE, None),
        ("settings.json", b'{"a": ', UnitKind.LINE, None),
        ("settings.json", b'{"a": 1, "a": 2}', UnitKind.LINE, None),
    ],
)
def test_engine_for_a_file_without_rows_follows_its_format_and_base(
    name: str, base: bytes, kind: UnitKind, fmt: StructuredFormat | None
) -> None:
    engine = engine_for(Path("live") / name, base, [])

    assert (engine.kind, engine.fmt) == (kind, fmt)
    assert engine.supports_adopt is (kind is UnitKind.LINE)
    assert engine == engine_for(Path("elsewhere") / name, base, [])


@pytest.mark.parametrize("base", [_PARSES, _BROKEN], ids=["parses", "broken"])
@pytest.mark.parametrize(
    ("rows", "kind"),
    [([_LINE_ROW], UnitKind.LINE), ([_KEY_ROW], UnitKind.KEY)],
    ids=["line-rows", "key-rows"],
)
def test_engine_for_keeps_the_kind_of_the_stored_rows(
    base: bytes, rows: list[dict[str, object]], kind: UnitKind
) -> None:
    assert engine_for(Path("settings.yaml"), base, rows).kind is kind


@pytest.mark.parametrize("base", [_PARSES, _BROKEN], ids=["parses", "broken"])
def test_engine_for_a_plain_file_ignores_key_rows(base: bytes) -> None:
    engine = engine_for(Path("notes.md"), base, [_KEY_ROW])

    assert engine is LINE
    with pytest.raises(InvariantViolation, match="incompatible with current 'line'"):
        require_unit_kind([_KEY_ROW], engine.kind)


def test_engine_for_a_structured_file_rejects_rows_of_mixed_kinds() -> None:
    rows = [_LINE_ROW, _KEY_ROW]
    engine = engine_for(Path("settings.yaml"), _BROKEN, rows)

    assert engine.kind is UnitKind.KEY
    with pytest.raises(InvariantViolation, match="incompatible with current 'key'"):
        require_unit_kind(rows, engine.kind)


@pytest.mark.parametrize(
    "engine", [LINE, structured_engine(StructuredFormat.YAML)], ids=["line", "key"]
)
def test_engine_stages_and_promotes_a_shared_unit(engine: UnitEngine[Any]) -> None:
    base, live = b"a: 1\nb: 2\n", b"a: 1\nb: 3\n"
    (unit,) = engine.extract(base, live)
    shared = [replace(unit, cls=HunkClass.SHARED)]

    rows = engine.serialize(shared, Path("settings.yaml"))
    assert [row["kind"] for row in rows] == [engine.kind.value]
    (carried,) = engine.classify([unit], rows)
    assert (carried.cls, carried.changed) == (HunkClass.SHARED, False)
    assert engine.bind_drafts(shared, {}) == {}
    assert engine.reconstruct(base, live, shared, {}) == live
    engine.assert_stage_fidelity(base, live, live, shared, {})
    with pytest.raises(InvariantViolation, match="INV-8"):
        engine.assert_stage_fidelity(base, live, base, shared, {})
    assert "3" in engine.preview(base, live, unit)


def test_line_engine_mints_relocation_anchors_only_for_markdown_sources() -> None:
    (hunk,) = LINE.extract(b"", b"## Mine\nbody\n")
    local = [replace(hunk, cls=HunkClass.LOCAL)]

    assert LINE.serialize(local, Path("notes.md"))[0]["reloc_anchor"] == "## Mine"
    assert "reloc_anchor" not in LINE.serialize(local, Path("notes.conf"))[0]


def test_content_hash_names_the_persisted_hash_of_either_unit_kind() -> None:
    (hunk,) = extract_hunks(b"a\n", b"b\n")
    (unit,) = extract_structured_units(b"a: 1\n", b"a: 2\n", StructuredFormat.YAML)

    assert hunk.content_hash == hunk.live_hash
    assert unit.content_hash == unit.value_hash
