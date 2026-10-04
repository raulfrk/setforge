"""The per-format staging engine and the unit hash name both kinds share."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from setforge.errors import InvariantViolation
from setforge.reconcile.hunks import extract_hunks
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


@pytest.mark.parametrize(
    ("name", "kind", "fmt"),
    [
        ("notes.md", UnitKind.LINE, None),
        ("settings.jsonc", UnitKind.LINE, None),
        ("settings.yaml", UnitKind.KEY, StructuredFormat.YAML),
        ("settings.yml", UnitKind.KEY, StructuredFormat.YAML),
        ("settings.json", UnitKind.KEY, StructuredFormat.JSONC),
    ],
)
def test_engine_for_routes_by_live_path_format(
    name: str, kind: UnitKind, fmt: StructuredFormat | None
) -> None:
    engine = engine_for(Path("live") / name)

    assert (engine.kind, engine.fmt) == (kind, fmt)
    assert engine.supports_adopt is (kind is UnitKind.LINE)
    assert engine == engine_for(Path("elsewhere") / name)


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
