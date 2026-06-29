"""Per-key staging model for structured (YAML/JSON/JSONC) files — the structured
analog of :mod:`setforge.reconcile.hunks`.

Where the line model extracts base↔live diff *hunks* keyed by a content+context
anchor, this extracts per-KEY units keyed by a **dotted leaf path** (path-only
identity; composite identity is the follow-up bead). A unit is classified by the
same :class:`~setforge.reconcile.types.HunkClass`, stored in the same index, and
reconstructed **through the model + re-serialize** (never text substitution) so
comments, anchors, quoting, and key order survive the round-trip.

A **leaf module** like :mod:`setforge.reconcile.hunks`: it does NOT import the
store (the caller wires all I/O).
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from ruamel.yaml import YAML

from setforge.errors import InvariantViolation
from setforge.reconcile.types import HunkClass
from setforge.structural_merge import (
    get_node_at_path,
    set_at_path,
    set_node_at_path,
)

#: YAML dump width set high so a long scalar is never reflowed onto a new line —
#: a reflow would mint a phantom diff on an untouched unit (smell SP5).
_YAML_WIDTH: Final = 4096


class StructuredFormat(StrEnum):
    """The structured file format a key-unit operation runs against."""

    YAML = "yaml"
    JSON = "json"
    JSONC = "jsonc"


@dataclass(frozen=True, slots=True)
class KeyUnit:
    """One structured leaf-key unit.

    ``path`` is the dotted leaf path and the **identity** (path-only). ``cls`` is
    the staging classification; ``label`` is the human handle (the path itself);
    ``value_hash`` is the sha256 of the normalised live leaf value, consulted to
    flag a value edit since the unit was classified. ``draft_hash`` is set only
    for a ``SHARED_DRAFTED`` unit.
    """

    cls: HunkClass
    label: str
    path: str
    value_hash: str
    changed: bool = False
    draft_hash: str | None = None


def identity(unit: KeyUnit) -> str:
    """The stable identity of a key-unit: its dotted ``path`` (path-only)."""
    return unit.path


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _yaml() -> YAML:
    """A ruamel round-trip YAML configured for byte-faithful preserve.

    ``preserve_quotes`` keeps a scalar's quote style; ``width`` is set high so a
    long scalar is never reflowed onto a new line (smell SP5).
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = _YAML_WIDTH
    return yaml


def _load_model(data: bytes, fmt: StructuredFormat) -> object:
    """Parse ``data`` into a fresh comment-preserving model for ``fmt``."""
    text = data.decode("utf-8")
    if fmt is StructuredFormat.YAML:
        return _yaml().load(io.StringIO(text))
    from json5 import loads as _json5_loads
    from json5.loader import ModelLoader

    return _json5_loads(text, loader=ModelLoader())


def _dump_model(model: object, fmt: StructuredFormat) -> bytes:
    """Serialise ``model`` back to byte-faithful text for ``fmt``."""
    if fmt is StructuredFormat.YAML:
        buf = io.StringIO()
        _yaml().dump(model, buf)
        return buf.getvalue().encode("utf-8")
    from json5.dumper import ModelDumper
    from json5.dumper import dumps as _json5_dumps

    return _json5_dumps(model, dumper=ModelDumper()).encode("utf-8")


class _Missing:
    """Sentinel for a leaf absent on one side (distinct from a present null)."""


_MISSING: Final = _Missing()


def _own_items(node: Mapping[object, object]) -> Iterator[tuple[object, object]]:
    """Yield only a mapping's OWN (physically-present) items.

    For a ruamel ``CommentedMap`` this uses ``non_merged_items()`` so a YAML
    ``<<`` merge key does NOT surface inherited keys as if they were the
    mapping's own — surfacing an inherited key would mint a phantom unit and a
    reconstruct that writes it would break the merge reference (smell SP4). A
    json-five ``JSONObject`` / plain ``dict`` has no merge keys, so ``.items()``.
    """
    non_merged = getattr(node, "non_merged_items", None)
    if callable(non_merged):
        yield from non_merged()
    else:
        yield from node.items()


def _walk_leaves(node: object, prefix: str = "") -> Iterator[tuple[str, object]]:
    """Yield ``(dotted_path, plain_value)`` for every LEAF under ``node``.

    A leaf is any non-mapping value. Mapping keys extend the dotted prefix; only
    a mapping's OWN keys are walked (see :func:`_own_items`).
    """
    if isinstance(node, Mapping):
        for key, value in _own_items(node):
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_leaves(value, path)
    else:
        yield (prefix, node)


def extract_structured_units(
    base: bytes, live: bytes, fmt: StructuredFormat
) -> list[KeyUnit]:
    """Extract the per-key base↔live diff units (each unclassified → PENDING).

    Enumerates the union of leaf paths in both models; every path whose base and
    live values differ becomes one PENDING :class:`KeyUnit`. An empty result
    means live equals base (nothing to stage).
    """
    base_leaves = dict(_walk_leaves(_load_model(base, fmt)))
    live_leaves = dict(_walk_leaves(_load_model(live, fmt)))
    units: list[KeyUnit] = []
    for path in sorted(set(base_leaves) | set(live_leaves)):
        base_value = base_leaves.get(path, _MISSING)
        live_value = live_leaves.get(path, _MISSING)
        if base_value == live_value:
            continue
        units.append(
            KeyUnit(
                cls=HunkClass.PENDING,
                label=path,
                path=path,
                value_hash=_sha(repr(live_value).encode("utf-8")),
            )
        )
    return units


def serialize_structured(units: list[KeyUnit]) -> list[dict[str, object]]:
    """Project key-units to their persisted index rows (``kind:"key"``).

    A key-unit row carries ``path`` + ``value_hash`` (its identity) where a line
    row carries ``anchor`` + ``live_hash``; the ``kind`` discriminator lets the
    fail-closed codec validate each shape. A ``SHARED_DRAFTED`` unit additionally
    carries its ``draft_hash`` (the draft *bytes* live in the ``drafts/`` store).
    """
    rows: list[dict[str, object]] = []
    for unit in units:
        row: dict[str, object] = {
            "kind": "key",
            "cls": unit.cls.value,
            "label": unit.label,
            "path": unit.path,
            "value_hash": unit.value_hash,
        }
        if unit.cls is HunkClass.SHARED_DRAFTED and unit.draft_hash is not None:
            row["draft_hash"] = unit.draft_hash
        rows.append(row)
    return rows


def _row_draft_hash(row: dict[str, object]) -> str | None:
    """The row's ``draft_hash`` (set on a ``SHARED_DRAFTED`` row), else ``None``."""
    value = row.get("draft_hash")
    return value if isinstance(value, str) else None


def classify_structured(
    fresh: list[KeyUnit], stored: list[dict[str, object]]
) -> list[KeyUnit]:
    """Carry stored classifications onto freshly-extracted units by PATH.

    A unit whose ``(path, value_hash)`` matches a stored row inherits its class. A
    unit whose ``path`` matches but whose ``value_hash`` changed keeps the stored
    class, flagged ``changed=True`` (surfaced for re-confirm, never silently
    reset). A ``SHARED_DRAFTED`` row matches by ``path`` alone (its tracked bytes
    come from the draft store, so the live value may be anything). Paths are
    unique within a file, so no anchor-collision guard is needed. Anything
    unmatched stays PENDING.
    """
    drafted_by_path = {
        str(r["path"]): r
        for r in stored
        if r.get("cls") == HunkClass.SHARED_DRAFTED.value
    }
    by_identity = {(str(r["path"]), str(r["value_hash"])): r for r in stored}
    by_path = {str(r["path"]): r for r in stored}
    out: list[KeyUnit] = []
    for unit in fresh:
        drafted = drafted_by_path.get(unit.path)
        if drafted is not None:
            out.append(
                replace(
                    unit,
                    cls=HunkClass.SHARED_DRAFTED,
                    changed=False,
                    draft_hash=_row_draft_hash(drafted),
                )
            )
            continue
        exact = by_identity.get((unit.path, unit.value_hash))
        if exact is not None:
            out.append(
                replace(
                    unit,
                    cls=HunkClass(str(exact["cls"])),
                    changed=False,
                    draft_hash=_row_draft_hash(exact),
                )
            )
            continue
        moved = by_path.get(unit.path)
        if moved is not None:
            out.append(
                replace(
                    unit,
                    cls=HunkClass(str(moved["cls"])),
                    changed=True,
                    draft_hash=_row_draft_hash(moved),
                )
            )
            continue
        out.append(unit)  # unmatched → PENDING (the extract default)
    return out


def _promotes(unit: KeyUnit) -> bool:
    """Whether a unit's **live** value is promoted into tracked on reconstruct.

    Only an exact-identity ``SHARED`` unit promotes its live value; a ``changed``
    unit (value edited since it was staged) is held at base until re-confirmed.
    """
    return unit.cls is HunkClass.SHARED and not unit.changed


def reconstruct_structured(
    base: bytes,
    live: bytes,
    units: list[KeyUnit],
    drafts: dict[str, bytes],
    fmt: StructuredFormat,
) -> bytes:
    """Rebuild tracked content as ``base`` with each promoted key's value spliced.

    A promoted ``SHARED`` unit takes its **live** value, set through the model via
    :func:`~setforge.structural_merge.set_node_at_path` (the comment/anchor/quote-
    preserving wrapped-node splice) and re-serialised — never text substitution, so
    an untouched unit round-trips byte-identical. Every LOCAL/PENDING/``changed``
    unit keeps its base value.
    """
    base_model = _load_model(base, fmt)
    live_model = _load_model(live, fmt)
    for unit in units:
        if unit.cls is HunkClass.SHARED_DRAFTED and not unit.changed:
            try:
                draft = drafts[unit.path]
            except KeyError as err:
                raise InvariantViolation(
                    f"SHARED_DRAFTED key-unit {unit.path!r} has no draft in the store"
                ) from err
            set_at_path(base_model, unit.path, draft.decode("utf-8"))
        elif _promotes(unit):
            node = get_node_at_path(live_model, unit.path)
            set_node_at_path(base_model, unit.path, node)
    return _dump_model(base_model, fmt)


def assert_stage_fidelity_structured(
    base: bytes,
    live: bytes,
    tracked: bytes,
    units: list[KeyUnit],
    drafts: dict[str, bytes],
    fmt: StructuredFormat,
) -> None:
    """INV-8: ``tracked`` must equal the reconstruct of exactly the promoted set.

    Raises :class:`~setforge.errors.InvariantViolation` when ``tracked`` carries a
    LOCAL/PENDING key's live value, is missing a SHARED one, or lacks a
    SHARED_DRAFTED key's draft — i.e. the committed tree is not exactly the
    shared/drafted set.
    """
    expected = reconstruct_structured(base, live, units, drafts, fmt)
    if tracked != expected:
        raise InvariantViolation(
            "INV-8: tracked content is not exactly the shared key-unit set "
            "(reconstruct_structured(...) != tracked)"
        )
