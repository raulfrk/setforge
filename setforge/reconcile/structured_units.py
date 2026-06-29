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
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ruamel.yaml import YAML

from setforge.reconcile.types import HunkClass
from setforge.structural_merge import get_node_at_path, set_node_at_path

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


def _walk_leaves(node: object, prefix: str = "") -> Iterator[tuple[str, object]]:
    """Yield ``(dotted_path, plain_value)`` for every LEAF under ``node``.

    A leaf is any non-mapping value. Mapping keys extend the dotted prefix.
    """
    if isinstance(node, Mapping):
        for key, value in node.items():
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
        if _promotes(unit):
            node = get_node_at_path(live_model, unit.path)
            set_node_at_path(base_model, unit.path, node)
    return _dump_model(base_model, fmt)
