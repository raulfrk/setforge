"""The staging engine of one file: line hunks or structured key units.

Staging, capture and compare run the same steps on either kind of unit —
extract the base↔live units, carry the stored classifications onto them, bind
drafts, reconstruct the tracked content, assert INV-8, serialise the index rows.
:func:`engine_for` picks the engine by the live path's format, so a caller is
written once against :class:`UnitEngine`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache, partial
from pathlib import Path
from typing import Any, Final

from setforge.reconcile import hunks
from setforge.reconcile import structured_units as su
from setforge.reconcile.hunks import Hunk
from setforge.reconcile.merge import split_lines
from setforge.reconcile.structured_units import KeyUnit, StructuredFormat
from setforge.reconcile.types import UnitKind, UnitRef

type Drafts = dict[UnitRef, bytes]
type Rows = list[dict[str, object]]

#: Longest unit preview shown in the ``stage`` walk before truncation.
_PREVIEW_MAX: Final = 600


@dataclass(frozen=True, slots=True)
class UnitEngine[U: (Hunk, KeyUnit)]:
    """The staging operations of one unit kind, bound to a file's format.

    ``serialize`` takes the tracked source path: only a Markdown source may mint
    a relocation anchor. ``supports_adopt`` is whether ``stage`` can rewrite the
    live file to an accepted draft.
    """

    kind: UnitKind
    fmt: StructuredFormat | None
    noun: str
    supports_adopt: bool
    extract: Callable[[bytes, bytes], list[U]]
    classify: Callable[[list[U], Rows], list[U]]
    bind_drafts: Callable[[list[U], Drafts], Drafts]
    reconstruct: Callable[[bytes, bytes, list[U], Drafts], bytes]
    assert_stage_fidelity: Callable[[bytes, bytes, bytes, list[U], Drafts], None]
    serialize: Callable[[list[U], Path], Rows]
    preview: Callable[[bytes, bytes, U], str]


def _clip(body: str) -> str:
    return body if len(body) <= _PREVIEW_MAX else body[: _PREVIEW_MAX - 1] + "…"


def _hunk_preview(base: bytes, live: bytes, hunk: Hunk) -> str:
    """A small ±diff preview of one hunk."""
    i1, i2 = hunk.base_span
    j1, j2 = hunk.live_span
    removed = [b"- " + line for line in split_lines(base)[i1:i2]]
    added = [b"+ " + line for line in split_lines(live)[j1:j2]]
    return _clip(b"".join(removed + added).decode("utf-8", "replace"))


def _key_preview(fmt: StructuredFormat, base: bytes, live: bytes, unit: KeyUnit) -> str:
    """A small base→live value preview of one key unit."""
    base_val = su.value_preview(base, unit.path, fmt)
    live_val = su.value_preview(live, unit.path, fmt)
    return _clip(f"  {unit.path}:\n- {base_val}\n+ {live_val}")


def _serialize_hunks(units: list[Hunk], src: Path) -> Rows:
    return hunks.serialize(
        units, allow_relocation=src.suffix.lower() in {".md", ".markdown"}
    )


def _serialize_keys(units: list[KeyUnit], src: Path) -> Rows:
    return su.serialize_structured(units)


LINE: Final[UnitEngine[Hunk]] = UnitEngine(
    kind=UnitKind.LINE,
    fmt=None,
    noun="hunk",
    supports_adopt=True,
    extract=hunks.extract_hunks,
    classify=hunks.classify,
    bind_drafts=hunks.bind_drafts,
    reconstruct=hunks.reconstruct,
    assert_stage_fidelity=hunks.assert_stage_fidelity,
    serialize=_serialize_hunks,
    preview=_hunk_preview,
)


@cache
def structured_engine(fmt: StructuredFormat) -> UnitEngine[KeyUnit]:
    """The key-unit engine for ``fmt`` (one shared value per format)."""
    return UnitEngine(
        kind=UnitKind.KEY,
        fmt=fmt,
        noun="key",
        supports_adopt=False,
        extract=partial(su.extract_structured_units, fmt=fmt),
        classify=partial(su.classify_structured, fmt=fmt),
        bind_drafts=su.bind_structured_drafts,
        reconstruct=partial(su.reconstruct_structured, fmt=fmt),
        assert_stage_fidelity=partial(su.assert_stage_fidelity_structured, fmt=fmt),
        serialize=_serialize_keys,
        preview=partial(_key_preview, fmt),
    )


def engine_for(dst: Path) -> UnitEngine[Any]:
    """The engine that stages the file deployed at ``dst``.

    The unit type is erased because the format is only known at run time; units
    an engine produces are only ever handed back to that same engine.
    """
    fmt = su.structured_format(dst)
    return LINE if fmt is None else structured_engine(fmt)
