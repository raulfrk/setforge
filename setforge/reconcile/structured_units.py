"""Per-key staging model for structured (YAML/JSON/JSONC) files — the structured
analog of :mod:`setforge.reconcile.hunks`.

Where the line model extracts base↔live diff *hunks* keyed by a content+context
anchor, this extracts per-KEY units keyed by a **dotted leaf path** (path-only
identity; composite identity was considered and REJECTED as a non-goal for v1 —
see ``docs/RULES.md`` DEC-1). A unit is classified by the
same :class:`~setforge.reconcile.types.HunkClass`, stored in the same index, and
reconstructed **through the model + re-serialize** (never text substitution) so
comments, anchors, quoting, and key order survive the round-trip.

A **leaf module** like :mod:`setforge.reconcile.hunks`: it does NOT import the
store (the caller wires all I/O).
"""

from __future__ import annotations

import io
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.nodes import ScalarNode

from setforge.errors import (
    DraftConfinementError,
    InvariantViolation,
    StructuredParseError,
)
from setforge.reconcile.index_model import KIND_KEY
from setforge.reconcile.types import HunkClass, UnitRef, content_sha
from setforge.scalar_merge import ABSENT
from setforge.structural_merge import (
    _plain_eq,
    append_key_segment,
    delete_node_at_path,
    get_at_path,
    get_node_at_path,
    join_key_segments,
    set_at_path,
    set_node_at_path,
    split_key_path,
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

    ``path`` is the dotted leaf path and the **identity** (path-only; a composite
    path+fingerprint identity was considered and REJECTED as a non-goal for v1 —
    see ``docs/RULES.md`` DEC-1). ``cls`` is
    the staging classification; ``label`` is the human handle (the path itself);
    ``value_hash`` is the sha256 of ``repr()`` of the live leaf value (see
    :func:`extract_structured_units`, which hashes ``repr(live_value)``),
    consulted to flag a value edit since the unit was classified.
    ``confirmed_hash`` transiently carries the persisted last-confirmed
    ``value_hash`` while ``value_hash`` describes the current live value;
    serialization preserves the former until an explicit stage decision replaces
    it. ``draft_hash`` is set only for a ``SHARED_DRAFTED`` unit.
    """

    cls: HunkClass
    label: str
    path: str
    value_hash: str
    changed: bool = False
    confirmed_hash: str | None = None
    draft_hash: str | None = None
    legacy_path: str | None = None

    @property
    def ref(self) -> UnitRef:
        return UnitRef.key(self.path)


def _yaml() -> YAML:
    """A ruamel round-trip YAML configured for byte-faithful preserve.

    ``preserve_quotes`` keeps a scalar's quote style; ``width`` is
    :data:`_YAML_WIDTH` (see its rationale — no long-scalar reflow, smell SP5).
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = _YAML_WIDTH
    return yaml


def _load_model(data: bytes, fmt: StructuredFormat) -> object:
    """Parse ``data`` into a fresh comment-preserving model for ``fmt``.

    Wraps a decode / parse failure into
    :class:`~setforge.errors.StructuredParseError` so no raw
    ``UnicodeDecodeError``, ruamel ``YAMLError`` (incl. a ``ComposerError`` for a
    multi-document stream), or json5 parse exception escapes to a caller. The
    stage walk catches it to fall back to line-level staging.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        raise StructuredParseError(
            f"structured input is not valid UTF-8: {err}"
        ) from err
    # Hoisted above the try so a missing-dependency ImportError propagates as
    # itself, not mislabelled as an unparseable-input StructuredParseError.
    from json5 import loads as _json5_loads
    from json5.loader import ModelLoader

    try:
        if fmt is StructuredFormat.YAML:
            return _yaml().load(io.StringIO(text))
        return _json5_loads(text, loader=ModelLoader())
    except Exception as err:
        raise StructuredParseError(f"structured input is not parseable: {err}") from err


def _dump_model(model: object, fmt: StructuredFormat) -> bytes:
    """Serialise ``model`` back to byte-faithful text for ``fmt``."""
    try:
        if fmt is StructuredFormat.YAML:
            buf = io.StringIO()
            _yaml().dump(model, buf)
            return buf.getvalue().encode("utf-8")
        from json5.dumper import ModelDumper
        from json5.dumper import dumps as _json5_dumps

        return _json5_dumps(model, dumper=ModelDumper()).encode("utf-8")
    except StructuredParseError:
        raise
    except Exception as err:
        raise StructuredParseError(
            f"structured model is not serialisable: {err}"
        ) from err


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


def _walk_leaves(
    node: object, prefix: str | None = None
) -> Iterator[tuple[str, object]]:
    """Yield ``(dotted_path, plain_value)`` for every unit under ``node``.

    A unit is a non-mapping leaf or an empty mapping container. Mapping keys
    extend the dotted prefix via
    :func:`~setforge.structural_merge.append_key_segment`, whose injective
    encoding escapes a literal ``.`` (or ``\\``) inside a key — so a flat key
    named ``"a.b"`` and a nested ``a: {b: …}`` yield DISTINCT paths instead of
    colliding on ``"a.b"``. The root call passes ``prefix=None`` (a real
    sentinel), so a GENUINE empty-string mapping key stays distinct from the root
    instead of both reading as "no prefix" — otherwise ``{"": {"a": 1}}`` would
    collapse onto ``{"a": 1}``. Only a mapping's OWN keys are walked (see
    :func:`_own_items`).
    """
    if isinstance(node, Mapping):
        items = list(_own_items(node))
        if not items and prefix is not None:
            yield (prefix, get_at_path(node, ""))
        for key, value in items:
            if not isinstance(key, str):
                raise StructuredParseError(
                    f"structured input has a non-string mapping key: {key!r} "
                    f"({type(key).__name__})"
                )
            path = append_key_segment(prefix, key)
            yield from _walk_leaves(value, path)
    else:
        # ``prefix is None`` only when the WHOLE document is a bare scalar (the
        # root call never descended through a key); its single leaf takes the
        # empty path, matching the pre-sentinel behavior. A leaf reached through
        # any key always carries a real dotted-string prefix.
        yield (prefix if prefix is not None else "", get_at_path(node, ""))


def extract_structured_units(
    base: bytes, live: bytes, fmt: StructuredFormat
) -> list[KeyUnit]:
    """Extract the per-key base↔live diff units (each unclassified → PENDING).

    YAML enumerates the union of leaf and empty-container paths in both models.
    JSON/JSONC remains
    intentionally opaque and therefore produces at most one whole-document unit
    at ``path == ""``. Every differing path becomes one PENDING
    :class:`KeyUnit`; an empty result means live equals base (nothing to stage).
    """
    base_leaves = dict(_walk_leaves(_load_model(base, fmt)))
    live_leaves = dict(_walk_leaves(_load_model(live, fmt)))
    units: list[KeyUnit] = []
    for path in sorted(set(base_leaves) | set(live_leaves)):
        base_value = base_leaves.get(path, _MISSING)
        live_value = live_leaves.get(path, _MISSING)
        if _plain_eq(base_value, live_value):
            continue
        units.append(
            KeyUnit(
                cls=HunkClass.PENDING,
                label=path,
                path=path,
                value_hash=content_sha(repr(live_value).encode("utf-8")),
            )
        )
    return units


def serialize_structured(units: list[KeyUnit]) -> list[dict[str, object]]:
    """Project key-units to their persisted index rows (``kind:"key"``).

    A matched key-unit preserves its transient ``confirmed_hash`` as the
    persisted ``value_hash`` until an explicit stage decision confirms the
    current value. A key-unit row carries ``path`` + ``value_hash`` where a line
    row carries ``anchor`` + ``live_hash``; the ``kind`` discriminator lets the
    fail-closed codec validate each shape. A ``SHARED_DRAFTED`` unit additionally
    carries its ``draft_hash`` (the draft *bytes* live in the ``drafts/`` store).
    """
    rows: list[dict[str, object]] = []
    for unit in units:
        row: dict[str, object] = {
            "kind": KIND_KEY,
            "cls": unit.cls.value,
            "label": unit.label,
            "path": unit.path,
            "value_hash": (
                unit.confirmed_hash
                if unit.confirmed_hash is not None
                else unit.value_hash
            ),
        }
        if unit.cls is HunkClass.SHARED_DRAFTED and unit.draft_hash is not None:
            row["draft_hash"] = unit.draft_hash
        rows.append(row)
    return rows


def _row_draft_hash(row: dict[str, object]) -> str | None:
    """The row's ``draft_hash`` (set on a ``SHARED_DRAFTED`` row), else ``None``."""
    value = row.get("draft_hash")
    return value if isinstance(value, str) else None


def _normalize_legacy_yaml_paths(
    fresh: list[KeyUnit],
    rows: list[dict[str, object]],
    fmt: StructuredFormat | None,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """Alias uniquely-shaped pre-codec YAML empty keys to canonical paths."""
    if fmt is not StructuredFormat.YAML:
        return rows, {}
    fresh_paths = {unit.path for unit in fresh}
    normalized: list[dict[str, object]] = []
    legacy_paths: dict[str, str] = {}
    for row in rows:
        old_path = str(row["path"])
        segments = split_key_path(old_path)
        canonical = join_key_segments(segments)
        if "" in segments and canonical != old_path and canonical in fresh_paths:
            candidates = fresh_paths & {old_path, canonical}
            if len(candidates) != 1:
                raise InvariantViolation(
                    f"legacy YAML key path {old_path!r} is ambiguous with a "
                    "document-root or shape-transition unit"
                )
            legacy_paths[canonical] = old_path
            row = {**row, "path": canonical}
        normalized.append(row)
    return normalized, legacy_paths


def classify_structured(
    fresh: list[KeyUnit],
    stored: list[dict[str, object]],
    fmt: StructuredFormat | None = None,
) -> list[KeyUnit]:
    """Carry stored classifications onto freshly-extracted units by PATH.

    A unit whose ``(path, value_hash)`` matches a stored row inherits its class. A
    unit whose ``path`` matches but whose ``value_hash`` changed keeps the stored
    class, flagged ``changed=True`` (surfaced for re-confirm, never silently
    reset). A ``SHARED_DRAFTED`` row matches by ``path`` alone (its tracked bytes
    come from the draft store, so the live value may be anything). Paths are
    unique within a file; duplicate fresh or stored paths fail closed rather than
    letting a dict comprehension pick an arbitrary unit. Anything unmatched stays
    PENDING.

    Only ``kind:"key"`` rows carry a ``path`` + ``value_hash`` identity, so the
    stored list is filtered to key-rows first — a stray line-row (``anchor`` +
    ``live_hash``, no ``path``) sharing the entry can never raise an unwrapped
    ``KeyError`` here.
    """
    key_rows = [r for r in stored if r.get("kind") == KIND_KEY]
    key_rows, legacy_paths = _normalize_legacy_yaml_paths(fresh, key_rows, fmt)
    for source, paths in (
        ("fresh", [unit.path for unit in fresh]),
        ("stored", [str(row["path"]) for row in key_rows]),
    ):
        duplicate = next(
            (path for path, count in Counter(paths).items() if count > 1), None
        )
        if duplicate is not None:
            raise InvariantViolation(
                f"duplicate {source} structured key path {duplicate!r}"
            )
    drafted_by_path = {
        str(r["path"]): r
        for r in key_rows
        if r.get("cls") == HunkClass.SHARED_DRAFTED.value
    }
    by_identity = {(str(r["path"]), str(r["value_hash"])): r for r in key_rows}
    by_path = {str(r["path"]): r for r in key_rows}
    out: list[KeyUnit] = []
    for unit in fresh:
        drafted = drafted_by_path.get(unit.path)
        if drafted is not None:
            out.append(
                replace(
                    unit,
                    cls=HunkClass.SHARED_DRAFTED,
                    changed=False,
                    confirmed_hash=str(drafted["value_hash"]),
                    draft_hash=_row_draft_hash(drafted),
                    legacy_path=legacy_paths.get(unit.path),
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
                    confirmed_hash=str(exact["value_hash"]),
                    draft_hash=_row_draft_hash(exact),
                    legacy_path=legacy_paths.get(unit.path),
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
                    confirmed_hash=str(moved["value_hash"]),
                    draft_hash=_row_draft_hash(moved),
                    legacy_path=legacy_paths.get(unit.path),
                )
            )
            continue
        out.append(unit)  # unmatched → PENDING (the extract default)
    return out


def bind_structured_drafts(
    units: list[KeyUnit], drafts: dict[UnitRef, bytes]
) -> dict[UnitRef, bytes]:
    """Bind an old YAML empty-key draft identity to its canonical fresh path."""
    bound = dict(drafts)
    for unit in units:
        if unit.legacy_path is None:
            continue
        legacy_ref = UnitRef.key(unit.legacy_path)
        if legacy_ref not in bound:
            continue
        if unit.ref in bound:
            raise InvariantViolation(
                f"draft store contains both legacy and canonical keys for {unit.path!r}"
            )
        bound[unit.ref] = bound.pop(legacy_ref)
    return bound


def _promotes(unit: KeyUnit) -> bool:
    """Whether a unit's **live** value is promoted into tracked on reconstruct.

    Only an exact-identity ``SHARED`` unit promotes its live value; a ``changed``
    unit (value edited since it was staged) is held at base until re-confirmed.
    """
    return unit.cls is HunkClass.SHARED and not unit.changed


def _has_promoted_intent(unit: KeyUnit) -> bool:
    """Whether reconstruction intends to replace this unit's base value."""
    return unit.cls in (HunkClass.SHARED, HunkClass.SHARED_DRAFTED) and not unit.changed


def _validated_reconstruction_order(
    units: list[KeyUnit], *, document_root: bool = False
) -> list[KeyUnit]:
    """Validate overlaps and return an ancestor-before-descendant order.

    A structural shape change can yield overlapping leaf paths: replacing
    ``a: {b: 1}`` with ``a: 2`` produces an added ``a`` unit and a deleted
    ``a.b`` unit.  Those units form one coherent change only when both are
    promoted or both are kept at base.  Applying just one side cannot preserve
    the other classification, so fail closed instead of silently losing intent.
    A promoted scalar draft cannot safely compose with any promoted overlapping
    unit, so that shape is rejected too.

    Coherent all-SHARED shape changes apply ancestors first. This makes both
    mapping-to-scalar and scalar-to-mapping reconstruction independent of the
    caller's unit order while preserving input order among equal-depth units.
    """
    paths = [
        (
            unit,
            ()
            if document_root and unit.path == ""
            else tuple(split_key_path(unit.path)),
        )
        for unit in units
    ]
    for index, (left, left_segments) in enumerate(paths):
        for right, right_segments in paths[index + 1 :]:
            shorter, longer = (
                (left_segments, right_segments)
                if len(left_segments) < len(right_segments)
                else (right_segments, left_segments)
            )
            if len(shorter) == len(longer) or longer[: len(shorter)] != shorter:
                continue
            if _has_promoted_intent(left) != _has_promoted_intent(right):
                raise StructuredParseError(
                    "structured units have incompatible parent/descendant intent: "
                    f"{left.path!r} is {left.cls.value}, "
                    f"{right.path!r} is {right.cls.value}"
                )
            if _has_promoted_intent(left) and (
                left.cls is HunkClass.SHARED_DRAFTED
                or right.cls is HunkClass.SHARED_DRAFTED
            ):
                raise StructuredParseError(
                    "drafted structured unit overlap cannot be reconstructed safely: "
                    f"{left.path!r} overlaps {right.path!r}"
                )
    return [unit for unit, _segments in sorted(paths, key=lambda item: len(item[1]))]


def _reconstruct_document_root(
    root: KeyUnit,
    drafts: dict[UnitRef, bytes],
    fmt: StructuredFormat,
    *,
    base: bytes,
    live: bytes,
) -> bytes:
    """Resolve one explicit whole-document unit without dotted-path mutation."""
    if root.cls is HunkClass.SHARED_DRAFTED and not root.changed:
        try:
            draft = drafts[root.ref]
        except KeyError as err:
            raise InvariantViolation(
                "SHARED_DRAFTED document-root unit has no draft in the store"
            ) from err
        parsed = parse_scalar_draft(draft, fmt)
        # json-five's model dumper does not accept a bare Python scalar. The
        # bounded parser above already proved confinement, so an opaque JSON
        # root draft can retain its validated bytes exactly.
        return draft if fmt is StructuredFormat.JSONC else _dump_model(parsed, fmt)
    return live if _promotes(root) else base


def _materialize_yaml_parents(
    target: object, live: object, path: str, fmt: StructuredFormat
) -> None:
    """Create absent mapping parents needed by a promoted nested YAML unit."""
    if fmt is not StructuredFormat.YAML:
        return
    segments = split_key_path(path)
    for depth in range(1, len(segments)):
        parent_path = join_key_segments(segments[:depth])
        if get_node_at_path(target, parent_path) is not ABSENT:
            continue
        if not isinstance(get_node_at_path(live, parent_path), Mapping):
            raise StructuredParseError(
                f"promoted nested unit {path!r} has no mapping parent "
                f"{parent_path!r} in live"
            )
        set_at_path(target, parent_path, {})


def reconstruct_structured(
    base: bytes,
    live: bytes,
    units: list[KeyUnit],
    drafts: dict[UnitRef, bytes],
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
    draft can; it never falls back to the live or base value. Incompatible
    parent/descendant classifications and a promoted path absent from both the
    original base and live models raise :class:`~setforge.errors.StructuredParseError`.
    """
    drafts = bind_structured_drafts(units, drafts)
    original_base_model = _load_model(base, fmt)
    live_model = _load_model(live, fmt)
    # JSON/JSONC is deliberately a single opaque root unit. For YAML, legacy
    # persisted path ``""`` remains interpretable: mapping↔mapping means the old
    # empty-string key, while any scalar root makes it a document replacement.
    document_root = any(unit.path == "" for unit in units) and (
        fmt is StructuredFormat.JSONC
        or not isinstance(original_base_model, Mapping)
        or not isinstance(live_model, Mapping)
    )
    ordered_units = _validated_reconstruction_order(units, document_root=document_root)
    if document_root:
        root = next(unit for unit in ordered_units if unit.path == "")
        return _reconstruct_document_root(root, drafts, fmt, base=base, live=live)
    base_model = _load_model(base, fmt)
    for unit in ordered_units:
        # Before the canonical empty-key token existed, YAML mapping rows used
        # ``path:""`` for a genuine empty key. ``document_root`` is false only
        # for mapping↔mapping YAML, which makes that old interpretation safe.
        operation_path = r"\0" if unit.path == "" else unit.path
        if unit.cls is HunkClass.SHARED_DRAFTED and not unit.changed:
            try:
                draft = drafts[unit.ref]
            except KeyError as err:
                raise InvariantViolation(
                    f"SHARED_DRAFTED key-unit {unit.path!r} has no draft in the store"
                ) from err
            _materialize_yaml_parents(base_model, live_model, operation_path, fmt)
            set_at_path(base_model, operation_path, parse_scalar_draft(draft, fmt))
        elif _promotes(unit):
            node = get_node_at_path(live_model, operation_path)
            if node is not ABSENT:
                _materialize_yaml_parents(base_model, live_model, operation_path, fmt)
                set_node_at_path(base_model, operation_path, node)
            elif get_node_at_path(original_base_model, operation_path) is not ABSENT:
                # A promoted SHARED unit whose LIVE value was deleted (key
                # present in base, absent in live): drop the leaf from base
                # (mirroring the line path's empty-span deletion) rather than
                # splice the ABSENT sentinel, which _dump_model cannot
                # serialise — the old behavior crashed sync/capture and left
                # the file uncapturable until the index was hand-edited. An
                # earlier promoted ancestor may already have removed this leaf;
                # in that case the deletion is satisfied and is a benign no-op.
                if get_node_at_path(base_model, operation_path) is not ABSENT:
                    delete_node_at_path(base_model, operation_path)
            else:
                # The path addressed no leaf in the IMMUTABLE original base or
                # live model, so this is a genuinely malformed/stale unit rather
                # than a deletion subsumed by an earlier ancestor replacement.
                # Preserve INV-8's fail-closed residual guard.
                raise StructuredParseError(
                    f"promoted SHARED unit {unit.path!r} addresses no leaf in "
                    f"base or live; cannot reconstruct"
                )
    return _dump_model(base_model, fmt)


def assert_stage_fidelity_structured(
    base: bytes,
    live: bytes,
    tracked: bytes,
    units: list[KeyUnit],
    drafts: dict[UnitRef, bytes],
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
