"""Per-key staging model for structured (YAML/JSON/JSONC) files — the structured
analog of :mod:`setforge.reconcile.hunks`.

Where the line model extracts base↔live diff *hunks* keyed by a content+context
anchor, this extracts per-KEY units keyed by a **dotted leaf path** (path-only
identity; composite identity is a later change). A unit is classified by the
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
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.nodes import ScalarNode

from setforge.errors import DraftConfinementError, InvariantViolation
from setforge.reconcile.types import HunkClass
from setforge.scalar_merge import ABSENT
from setforge.structural_merge import (
    get_at_path,
    get_node_at_path,
    set_at_path,
    set_node_at_path,
)

#: YAML dump width set high so a long scalar is never reflowed onto a new line —
#: a reflow would mint a phantom diff on an untouched unit (smell SP5).
_YAML_WIDTH: Final = 4096

#: C0 control chars (and DEL) forbidden in a draft scalar, minus tab/newline —
#: the same untrusted-output gate :mod:`setforge.reconcile.share_draft` applies to
#: line drafts, here enforced on BOTH the raw draft bytes and the parsed scalar.
_DRAFT_FORBIDDEN: Final = ({chr(c) for c in range(0x20)} - {"\t", "\n"}) | {"\x7f"}


class StructuredFormat(StrEnum):
    """The structured file format a key-unit operation runs against."""

    YAML = "yaml"
    JSON = "json"
    JSONC = "jsonc"


def structured_format(path: Path) -> StructuredFormat | None:
    """The structured format of ``path`` by suffix, or ``None`` for a plain file.

    ``.yaml`` / ``.yml`` → YAML; ``.json`` (per
    :func:`setforge.jsonc.is_jsonc_file`, which matches ``.json`` only) → JSONC
    via the json5 backend. Any other suffix — including ``.jsonc``, which
    ``is_jsonc_file`` does NOT match — returns ``None`` so the caller keeps the
    line-hunk path. The single source of truth for "is this a per-KEY-staged
    file?", shared by the stage walk (:mod:`setforge.cli.stage`) and capture
    (:mod:`setforge.capture`) so the two never disagree on which files stage
    structurally.
    """
    from setforge import jsonc

    if path.suffix in (".yaml", ".yml"):
        return StructuredFormat.YAML
    if jsonc.is_jsonc_file(path):
        return StructuredFormat.JSONC
    return None


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
    """Sentinel for a leaf absent on one side (distinct from a present null).

    A fixed ``__repr__`` is mandatory: ``extract_structured_units`` hashes
    ``repr(live_value)`` into a unit's ``value_hash`` for a deleted leaf, and that
    hash is persisted to the on-disk index and re-read in a later process. The
    default object repr embeds the instance's heap address (``0x...``), which
    differs across runs, so ``classify_structured`` could never re-match a
    deletion unit by identity. The constant marker below keeps the hash stable.
    """

    def __repr__(self) -> str:
        return "<setforge:_Missing>"


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
    an untouched unit round-trips byte-identical. A non-``changed`` ``SHARED_DRAFTED``
    unit instead takes its **draft-store** value, bounded-parsed into a typed scalar
    by :func:`parse_scalar_draft` and spliced via
    :func:`~setforge.structural_merge.set_at_path` so its type is preserved (a
    drafted ``22`` lands as an ``int``, not the string ``"22"``). Every other unit
    (LOCAL/PENDING, or any ``changed`` unit) keeps its base value.

    Raises :class:`~setforge.errors.InvariantViolation` (fail-closed) when a
    non-``changed`` ``SHARED_DRAFTED`` unit's ``path`` is absent from ``drafts`` (a
    dangling draft pointer), or :class:`~setforge.errors.DraftConfinementError` (a
    subclass) when a draft-store value escapes scalar confinement at splice — a
    corrupted/tampered store can no more inject structure than an interactive
    draft can; it never falls back to the live or base value.
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
            set_at_path(base_model, unit.path, parse_scalar_draft(draft, fmt))
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


def parse_scalar_draft(draft: bytes, fmt: StructuredFormat) -> object:
    """Bounded-parse a structured share-draft into a single typed scalar (SEC2-8).

    A structured key-unit draft replaces ONE scalar leaf. This is the
    type-confinement gate: the draft must be valid UTF-8, carry no forbidden
    control character (raw OR in the parsed string), and parse to exactly one
    SCALAR of the file's format — never a mapping/list (sibling/nesting
    injection) and never a YAML anchor/alias/merge construct (``&``/``*``/``<<``).
    The returned value keeps its parsed python type (``str``/``int``/``float``/
    ``bool``/``None``) so the caller can enforce a same-type match and
    :func:`reconstruct_structured` can splice the typed scalar (not a string).

    Raises :class:`~setforge.errors.DraftConfinementError` (a fail-closed
    :class:`~setforge.errors.InvariantViolation`) on any confinement breach.
    """
    try:
        text = draft.decode("utf-8")
    except UnicodeDecodeError as err:
        raise DraftConfinementError("draft is not valid UTF-8") from err
    if any(ch in _DRAFT_FORBIDDEN for ch in text):
        raise DraftConfinementError("draft carries a forbidden control character")
    value = (
        _parse_scalar_yaml(text)
        if fmt is StructuredFormat.YAML
        else _parse_scalar_json(text)
    )
    if isinstance(value, str) and any(ch in _DRAFT_FORBIDDEN for ch in value):
        raise DraftConfinementError(
            "parsed draft scalar carries a forbidden control character"
        )
    return value


def _parse_scalar_yaml(text: str) -> object:
    """Bounded YAML scalar parse: reject non-scalar shapes and ``&``/``*``/``<<``.

    Composes the node first so a YAML anchor on an otherwise-scalar draft
    (``&a 0``) is caught at the structural level — safe-loading alone would
    silently strip the anchor and hand back the bare value. A mapping (incl. a
    ``<<`` merge key) or sequence node is not a scalar; an undefined alias
    (``*x``) raises at compose time.
    """
    checker = YAML(typ="safe")
    try:
        node = checker.compose(text)
    except Exception as err:
        raise DraftConfinementError(f"draft is not parseable: {err}") from err
    if node is None:
        raise DraftConfinementError("draft is empty")
    if not isinstance(node, ScalarNode):
        raise DraftConfinementError(
            "draft is not a single scalar (parsed as a mapping/list/merge)"
        )
    anchor = getattr(node, "anchor", None)
    if getattr(anchor, "value", anchor):
        raise DraftConfinementError("draft carries a YAML anchor/alias (& / *)")
    # Reachable + security-relevant: a scalar-shaped but unsafe-tagged draft
    # (``!!python/...``, ``!!timestamp notadate``) passes the ScalarNode shape
    # check above, then the safe loader refuses the tag here — fail-closed.
    try:
        return YAML(typ="safe").load(text)
    except Exception as err:
        raise DraftConfinementError(f"draft is not parseable: {err}") from err


def _parse_scalar_json(text: str) -> object:
    """Bounded JSON/JSONC scalar parse: reject object/array shapes.

    JSON has no anchor/alias/merge constructs, so structure injection reduces to
    a parsed ``dict`` / ``list`` — rejected here so only a bare scalar survives.
    """
    from json5 import loads as _json5_loads

    try:
        value = _json5_loads(text)
    except Exception as err:
        raise DraftConfinementError(f"draft is not parseable: {err}") from err
    if isinstance(value, dict | list):
        raise DraftConfinementError(
            "draft is not a single scalar (parsed as an object/array)"
        )
    return value


def value_at(data: bytes, path: str, fmt: StructuredFormat) -> object:
    """The typed live value at dotted ``path`` (or :data:`ABSENT` when missing).

    The type anchor for a structured share-draft: a key-unit draft must parse to
    this value's exact type, so the interactive Share→Draft flow reads the live
    scalar here before handing it to
    :func:`~setforge.reconcile.share_draft.draft_key_unit`. Returns an unwrapped
    plain-python scalar (never a held ruamel/json-five node).
    """
    return get_at_path(_load_model(data, fmt), path)


def value_preview(data: bytes, path: str, fmt: StructuredFormat) -> str:
    """A short one-line string of the value at dotted ``path``, for a UI preview.

    Returns ``(absent)`` when the path is missing and ``(unparseable)`` on a parse
    failure — never raises, since a preview must not crash the interactive walk.
    """
    try:
        value = get_at_path(_load_model(data, fmt), path)
    except Exception:
        return "(unparseable)"
    if value is ABSENT:
        return "(absent)"
    text = repr(value)
    return text if len(text) <= 200 else text[:199] + "…"
