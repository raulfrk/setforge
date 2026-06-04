"""Base-aware structural 3-way merge over comment-preserving trees.

Generalizes the 2-way :func:`setforge.yaml_merge._deep_merge_dicts`
(``{live → tracked}``) into a true 3-way merge of ``{base, ours, theirs}``
where ``ours`` is the live model and ``theirs`` is the upstream model. The
``ours`` model is mutated IN PLACE to produce the merged output, so every
comment / anchor / key-order detail on untouched siblings survives. Diverged
keys that cannot auto-resolve accumulate as :class:`PathConflict` records
rather than picking a silent winner.

This module is additive: it does not touch the existing 2-way path or any
install/sync caller. It operates on already-parsed comment-preserving models:

* ruamel ``CommentedMap`` / ``CommentedSeq`` (YAML round-trip),
* the json-five model (``JSONObject`` etc., for JSONC), and
* plain ``dict`` / ``list`` for strict JSON.

Three concerns are kept separate:

* the format-agnostic 3-way decision per key (delegating scalar leaves to
  :func:`setforge.scalar_merge.resolve_scalar`),
* a wrapper-free, type-aware unwrap (:func:`_to_plain`) so the divergence
  test never trusts a permissive ``CommentedMap.__eq__`` (it ignores key
  order AND comments and treats ``1 == 1.0``), and
* a per-backend :class:`_MappingBackend` that does the in-place edits with
  correct comment provenance (the winning value carries its own side's
  attached comment).
"""

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Protocol

from json5.dumper import ModelDumper
from json5.dumper import dumps as _json5_dumps
from json5.loader import loads as _json5_loads
from json5.model import (
    JSONArray,
    JSONObject,
    JSONText,
    Value,
)
from ruamel.yaml.comments import CommentedMap

from setforge.errors import MergeTypeMismatch
from setforge.jsonc import _find_key_index
from setforge.scalar_merge import (
    ABSENT,
    ScalarOutcome,
    _scalar_eq,
    resolve_scalar,
)
from setforge.scalar_path import _set_jsonc_leaf

__all__ = [
    "PathConflict",
    "StructuralMergeResult",
    "merge_structural",
    "set_at_path",
]


@dataclass(frozen=True, slots=True)
class PathConflict:
    """A single key whose three sides diverge irreconcilably.

    ``base`` / ``ours`` / ``theirs`` are unwrapped plain-python values (or the
    :data:`setforge.scalar_merge.ABSENT` sentinel for a side where the key is
    missing), so a conflict record is comparable and printable without leaking
    a ruamel / json-five wrapper.
    """

    path: str
    base: object
    ours: object
    theirs: object


@dataclass(frozen=True, slots=True)
class StructuralMergeResult:
    """Outcome of :func:`merge_structural`.

    ``merged_model`` is the SAME object passed as ``ours``, mutated in place.
    ``clean`` is ``True`` iff ``conflicts`` is empty. Conflicted keys are left
    bearing ours' value in ``merged_model`` (no silent overwrite); the caller
    decides how to surface or resolve them.
    """

    clean: bool
    merged_model: object
    conflicts: list[PathConflict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Wrapper-free, type-aware unwrap for the divergence test.
# ---------------------------------------------------------------------------


def _to_plain(node: object) -> object:
    """Recursively unwrap ``node`` to plain ``dict`` / ``list`` / scalar.

    Used ONLY for equality / divergence decisions — never to build output.
    The result is compared with :func:`setforge.scalar_merge._scalar_eq`
    semantics (``1 != 1.0``, ``True != 1``, same-type NaN equal), which a raw
    ``CommentedMap.__eq__`` would violate (it ignores order/comments and
    treats ``1 == 1.0``).

    The :data:`ABSENT` sentinel passes through unchanged so an absent operand
    stays distinct from a present ``null``.
    """
    if node is ABSENT:
        return ABSENT
    if isinstance(node, JSONObject):
        return {
            _json5_key_text(kv.key): _to_plain(kv.value) for kv in node.key_value_pairs
        }
    if isinstance(node, JSONArray):
        return [_to_plain(elem) for elem in node.values]
    if _is_json5_scalar(node):
        return _json5_scalar_value(node)
    if isinstance(node, Mapping):
        return {key: _to_plain(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_to_plain(elem) for elem in node]
    return node


def _plain_eq(a: object, b: object) -> bool:
    """Type-aware deep equality over two already-unwrapped plain values.

    Recurses dict/list element-wise and defers scalar comparison (including
    the ``ABSENT`` sentinel) to :func:`setforge.scalar_merge._scalar_eq`, so
    the ``1 != 1.0`` / ``True != 1`` distinctions hold at every depth.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        return all(_plain_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(
            _plain_eq(x, y) for x, y in zip(a, b, strict=True)
        )
    # A container on one side and a scalar on the other are never equal;
    # _scalar_eq would raise on the container, so guard explicitly.
    if isinstance(a, dict | list) or isinstance(b, dict | list):
        return type(a) is type(b) and a == b
    return _scalar_eq(a, b)


# ---------------------------------------------------------------------------
# Shape classification.
# ---------------------------------------------------------------------------


def _is_mapping_node(node: object) -> bool:
    """Whether ``node`` is a mapping in any supported backend."""
    return isinstance(node, JSONObject | Mapping)


def _is_list_node(node: object) -> bool:
    """Whether ``node`` is a sequence in any supported backend."""
    return isinstance(node, JSONArray | list)


# ---------------------------------------------------------------------------
# Backend protocol: per-format in-place mapping edits.
# ---------------------------------------------------------------------------


class _MappingBackend(Protocol):
    """In-place editor over one mapping level, comment-provenance aware.

    Implementations wrap a single live (ours) mapping node and expose the
    other two sides' (base / theirs) sibling nodes so the merge can splice a
    winning value together with its own side's attached comment.
    """

    def keys(self) -> list[str]:
        """Return ours' keys in document order."""
        ...

    def has(self, side: str, key: str) -> bool:
        """Whether ``side`` (``"base"``/``"ours"``/``"theirs"``) has ``key``."""
        ...

    def raw(self, side: str, key: str) -> object:
        """Return the raw (wrapped) node for ``key`` on ``side``."""
        ...

    def take_ours(self, key: str) -> None:
        """Keep ours' value + comment unchanged (no-op marker)."""
        ...

    def take_theirs(self, key: str) -> None:
        """Replace ours' value at ``key`` with theirs' node AND comment."""
        ...

    def add(self, side: str, key: str) -> None:
        """Add ``key`` to ours from ``side``, carrying that side's comment."""
        ...

    def delete(self, key: str) -> None:
        """Delete ``key`` from ours."""
        ...

    def side_keys(self, side: str) -> list[str]:
        """Return ``side``'s keys in document order (empty if absent)."""
        ...


def _make_backend(base: object, ours: object, theirs: object) -> _MappingBackend:
    """Select the backend matching ours' mapping type."""
    if isinstance(ours, JSONObject):
        return _Json5Backend(base, ours, theirs)
    if isinstance(ours, CommentedMap | Mapping):
        return _RuamelBackend(base, ours, theirs)
    raise MergeTypeMismatch(
        f"unsupported mapping backend for ours: {type(ours).__name__}"
    )


# ---------------------------------------------------------------------------
# ruamel / plain-dict backend.
# ---------------------------------------------------------------------------


class _RuamelBackend:
    """Backend for ruamel ``CommentedMap`` and plain ``dict``.

    Comment provenance rides on the mapping's ``ca.items`` token table (a
    ``CommentedMap`` carries one; a plain ``dict`` does not, in which case the
    comment-copy steps are silent no-ops).
    """

    def __init__(self, base: object, ours: object, theirs: object) -> None:
        # ours is guaranteed a MutableMapping by _make_backend's dispatch.
        self._ours: MutableMapping[str, object] = ours  # type: ignore[assignment]
        self._sides: dict[str, Mapping[str, object]] = {"ours": self._ours}
        if isinstance(base, Mapping):
            self._sides["base"] = base
        if isinstance(theirs, Mapping):
            self._sides["theirs"] = theirs

    def keys(self) -> list[str]:
        return list(self._ours.keys())

    def side_keys(self, side: str) -> list[str]:
        node = self._sides.get(side)
        return list(node.keys()) if node is not None else []

    def has(self, side: str, key: str) -> bool:
        node = self._sides.get(side)
        return node is not None and key in node

    def raw(self, side: str, key: str) -> object:
        return self._sides[side][key]

    def take_ours(self, key: str) -> None:
        # ours already holds its value + comment; nothing to do.
        return

    def take_theirs(self, key: str) -> None:
        self._ours[key] = self._sides["theirs"][key]
        self._copy_comment(key)

    def add(self, side: str, key: str) -> None:
        self._ours[key] = self._sides[side][key]
        self._copy_comment(key, side)

    def delete(self, key: str) -> None:
        del self._ours[key]

    def _copy_comment(self, key: str, side: str = "theirs") -> None:
        """Move ``side``'s attached comment token onto ours at ``key``.

        Only meaningful for ``CommentedMap`` on both sides; plain dicts have
        no ``ca`` table so the guard makes this a no-op.
        """
        ours = self._ours
        src = self._sides[side]
        if not (isinstance(ours, CommentedMap) and isinstance(src, CommentedMap)):
            return
        if key in src.ca.items:
            ours.ca.items[key] = src.ca.items[key]
        elif key in ours.ca.items:
            # the winning side had no comment; drop ours' stale one so the
            # comment follows the winning value rather than lingering.
            del ours.ca.items[key]


# ---------------------------------------------------------------------------
# json-five backend.
# ---------------------------------------------------------------------------


class _Json5Backend:
    """Backend for the json-five model (``JSONObject``).

    ``JSONObject`` stores parallel ``keys`` / ``values`` lists;
    ``key_value_pairs`` is a DERIVED property (it re-zips ``keys``/``values``
    on every access), and the dumper iterates that property — so a mutation is
    visible only if it lands in BOTH ``keys`` and ``values``. Every edit here
    splices the two lists together to keep the derived view consistent.

    Comment provenance: a value's trailing ``//`` comment lives in that value
    node's ``wsc_after``, so a TAKE-theirs swaps the VALUE node only; theirs'
    trailing comment rides its value node's ``wsc_after``, leaving the key node
    in place (swapping the whole pair would clobber the preceding sibling's
    ``wsc_before``).
    """

    def __init__(self, base: object, ours: JSONObject, theirs: object) -> None:
        self._ours: JSONObject = ours
        self._sides: dict[str, JSONObject] = {"ours": ours}
        if isinstance(base, JSONObject):
            self._sides["base"] = base
        if isinstance(theirs, JSONObject):
            self._sides["theirs"] = theirs

    def keys(self) -> list[str]:
        return [_json5_key_text(k) for k in self._ours.keys]

    def side_keys(self, side: str) -> list[str]:
        node = self._sides.get(side)
        return [_json5_key_text(k) for k in node.keys] if node is not None else []

    def has(self, side: str, key: str) -> bool:
        node = self._sides.get(side)
        return node is not None and self._index(node, key) is not None

    def raw(self, side: str, key: str) -> object:
        node = self._sides[side]
        idx = self._index(node, key)
        assert idx is not None  # caller gates on has(); narrows for mypy
        return node.values[idx]

    def take_ours(self, key: str) -> None:
        return

    def take_theirs(self, key: str) -> None:
        theirs = self._sides["theirs"]
        t_idx = self._index(theirs, key)
        o_idx = self._index(self._ours, key)
        assert t_idx is not None
        assert o_idx is not None
        # Swap the VALUE node only. A value's own trailing comment (last-key
        # position) lives in its ``wsc_after`` and rides along; a post-comma
        # comment is structurally bound to the FOLLOWING key's ``wsc_before``
        # (json-five's separation), so swapping the key node would instead
        # clobber the preceding sibling's comment. Value-only is the stable,
        # provenance-correct unit. ``keys``/``values`` stay length-consistent,
        # so the derived ``key_value_pairs`` view follows automatically.
        self._ours.values[o_idx] = theirs.values[t_idx]

    def add(self, side: str, key: str) -> None:
        # Append ``key``'s key+value nodes from ``side`` onto ours; both lists
        # grow together so the derived key_value_pairs view stays consistent.
        src = self._sides[side]
        s_idx = self._index(src, key)
        assert s_idx is not None
        self._ours.keys.append(src.keys[s_idx])
        self._ours.values.append(src.values[s_idx])

    def delete(self, key: str) -> None:
        idx = self._index(self._ours, key)
        assert idx is not None
        # Keep keys / values consistent; key_value_pairs is derived so it
        # follows automatically on next access.
        del self._ours.keys[idx]
        del self._ours.values[idx]

    @staticmethod
    def _index(node: JSONObject, key: str) -> int | None:
        for i, k in enumerate(node.keys):
            if _json5_key_text(k) == key:
                return i
        return None


# ---------------------------------------------------------------------------
# json-five node helpers (kept local to this module per the jsonc.py rule).
# ---------------------------------------------------------------------------


def _json5_key_text(key_node: object) -> str:
    """Return a json-five key node's literal characters."""
    characters = getattr(key_node, "characters", None)
    if characters is not None:
        return str(characters)
    name = getattr(key_node, "name", None)
    if name is not None:
        return str(name)
    return str(key_node)


def _is_json5_scalar(node: object) -> bool:
    """Whether ``node`` is a json-five scalar leaf (not object/array)."""
    return isinstance(node, Value) and not isinstance(node, JSONObject | JSONArray)


def _json5_scalar_value(node: object) -> object:
    """Recover the plain-python value of a json-five scalar leaf.

    Integer/Float expose a typed ``.value`` (``int`` vs ``float``, keeping
    ``1`` distinct from ``1.0``); Boolean/Null likewise. String nodes expose
    ``.characters``. Anything unexpected falls back to a dump+reparse, which
    still yields the correctly-typed plain value.
    """
    if hasattr(node, "value") and not hasattr(node, "characters"):
        return node.value
    characters = getattr(node, "characters", None)
    if characters is not None:
        return str(characters)
    return _json5_loads(_json5_dumps(node, dumper=ModelDumper()))


# ---------------------------------------------------------------------------
# Core 3-way merge.
# ---------------------------------------------------------------------------


def merge_structural(
    base: object, ours: object, theirs: object
) -> StructuralMergeResult:
    """3-way merge ``{base, ours, theirs}`` over comment-preserving models.

    ``ours`` (the live model) is mutated IN PLACE and returned as
    ``merged_model``. Diverged keys that cannot auto-resolve are accumulated
    as :class:`PathConflict` records; ours' value is left in place for each.

    Raises :class:`~setforge.errors.MergeTypeMismatch` when a key is a mapping
    on one diverged side and a scalar/list on another (a true shape mismatch).
    """
    conflicts: list[PathConflict] = []
    # json-five hands back a ``JSONText`` wrapper; merge the inner objects in
    # place but return ours' wrapper so the result re-dumps with formatting.
    if isinstance(ours, JSONText):
        _merge_mapping(
            _json5_inner(base), ours.value, _json5_inner(theirs), "", conflicts
        )
        return StructuralMergeResult(
            clean=not conflicts, merged_model=ours, conflicts=conflicts
        )
    _merge_mapping(base, ours, theirs, "", conflicts)
    return StructuralMergeResult(
        clean=not conflicts, merged_model=ours, conflicts=conflicts
    )


def _json5_inner(model: object) -> object:
    """Unwrap a json-five ``JSONText`` to its inner value (passthrough else)."""
    return model.value if isinstance(model, JSONText) else model


def _merge_mapping(
    base: object,
    ours: object,
    theirs: object,
    prefix: str,
    conflicts: list[PathConflict],
) -> None:
    """Merge one mapping level in place, recursing into shared submaps."""
    backend = _make_backend(base, ours, theirs)
    for key in _union_keys(backend):
        _merge_key(backend, key, prefix, conflicts)


def _union_keys(backend: _MappingBackend) -> list[str]:
    """Ours' keys in order, then theirs-only keys (base-only keys are absent
    from ours by definition, so a base-only delete is implicit)."""
    ordered = backend.keys()
    seen = set(ordered)
    extra = [key for key in backend.side_keys("theirs") if key not in seen]
    return ordered + extra


def _merge_key(
    backend: _MappingBackend,
    key: str,
    prefix: str,
    conflicts: list[PathConflict],
) -> None:
    """Resolve a single key across the three sides."""
    path = f"{prefix}.{key}" if prefix else key
    b_present = backend.has("base", key)
    o_present = backend.has("ours", key)
    t_present = backend.has("theirs", key)

    b_raw = backend.raw("base", key) if b_present else ABSENT
    o_raw = backend.raw("ours", key) if o_present else ABSENT
    t_raw = backend.raw("theirs", key) if t_present else ABSENT

    # All three present AND all three mappings -> recurse.
    if (
        b_present
        and o_present
        and t_present
        and _is_mapping_node(b_raw)
        and _is_mapping_node(o_raw)
        and _is_mapping_node(t_raw)
    ):
        _merge_mapping(b_raw, o_raw, t_raw, path, conflicts)
        return

    _check_no_shape_mismatch(b_raw, o_raw, t_raw, path)
    _resolve_opaque(backend, key, path, b_raw, o_raw, t_raw, conflicts)


def _check_no_shape_mismatch(
    b_raw: object, o_raw: object, t_raw: object, path: str
) -> None:
    """Raise when two DIVERGED sides disagree on container-vs-scalar shape.

    A side that equals base never triggers a mismatch (it is not a competing
    edit). An ABSENT side is a delete, not a shape — it is resolved by the
    opaque take / conflict logic, never a type mismatch. Only when both
    ``ours`` and ``theirs`` are PRESENT, both changed away from base, AND one
    is a mapping/list while the other is a scalar do we refuse.
    """
    if o_raw is ABSENT or t_raw is ABSENT:
        return
    o_changed = not _plain_eq(_to_plain(o_raw), _to_plain(b_raw))
    t_changed = not _plain_eq(_to_plain(t_raw), _to_plain(b_raw))
    if not (o_changed and t_changed):
        return
    if _shape_tag(o_raw) != _shape_tag(t_raw):
        raise MergeTypeMismatch(
            f"type mismatch at {path!r}: ours is {_shape_tag(o_raw)}, "
            f"theirs is {_shape_tag(t_raw)}"
        )


def _shape_tag(node: object) -> str:
    """Coarse shape label for mismatch messages."""
    if node is ABSENT:
        return "absent"
    if _is_mapping_node(node):
        return "mapping"
    if _is_list_node(node):
        return "list"
    return "scalar"


def _resolve_opaque(
    backend: _MappingBackend,
    key: str,
    path: str,
    b_raw: object,
    o_raw: object,
    t_raw: object,
    conflicts: list[PathConflict],
) -> None:
    """Opaque whole-value 3-way for a non-recursing key.

    Pure scalars (and the ABSENT sentinel) on all present sides delegate to
    :func:`resolve_scalar`. Containers (lists, or a mapping that is not shared
    by all three) are compared whole via :func:`_to_plain` + :func:`_plain_eq`.
    """
    b_plain = _to_plain(b_raw)
    o_plain = _to_plain(o_raw)
    t_plain = _to_plain(t_raw)

    if _all_scalar(b_plain, o_plain, t_plain):
        _apply_scalar(backend, key, path, b_plain, o_plain, t_plain, conflicts)
        return

    # Container opaque take: ours==base -> theirs; theirs==base -> ours;
    # ours==theirs -> ours; else conflict.
    if _plain_eq(o_plain, b_plain):
        _apply_take(backend, key, "theirs", t_raw)
    elif _plain_eq(t_plain, b_plain):
        _apply_take(backend, key, "ours", o_raw)
    elif _plain_eq(o_plain, t_plain):
        backend.take_ours(key)
    else:
        conflicts.append(
            PathConflict(path=path, base=b_plain, ours=o_plain, theirs=t_plain)
        )


def _all_scalar(*plains: object) -> bool:
    """Whether every operand is a scalar or the ABSENT sentinel."""
    return all(p is ABSENT or not isinstance(p, dict | list) for p in plains)


def _apply_scalar(
    backend: _MappingBackend,
    key: str,
    path: str,
    b_plain: object,
    o_plain: object,
    t_plain: object,
    conflicts: list[PathConflict],
) -> None:
    """Delegate a scalar leaf to :func:`resolve_scalar` and apply the edit."""
    resolution = resolve_scalar(b_plain, o_plain, t_plain)
    match resolution.outcome:
        case ScalarOutcome.CONFLICT:
            conflicts.append(
                PathConflict(path=path, base=b_plain, ours=o_plain, theirs=t_plain)
            )
        case ScalarOutcome.DELETE:
            backend.delete(key)
        case ScalarOutcome.TAKE:
            _apply_scalar_take(backend, key, resolution.value, o_plain, t_plain)


def _apply_scalar_take(
    backend: _MappingBackend,
    key: str,
    value: object,
    o_plain: object,
    t_plain: object,
) -> None:
    """Apply a scalar TAKE, preferring node-level provenance when the chosen
    value matches an existing side's node (so its comment rides along)."""
    if not backend.has("ours", key):
        # ADD from theirs (ours lacked the key): splice theirs' node.
        backend.add("theirs", key)
        return
    if _scalar_eq(value, o_plain):
        backend.take_ours(key)
    elif _scalar_eq(value, t_plain) and backend.has("theirs", key):
        backend.take_theirs(key)
    else:
        backend.take_ours(key)


def _apply_take(backend: _MappingBackend, key: str, side: str, raw: object) -> None:
    """Apply an opaque take from ``side``, adding the key if ours lacks it."""
    if not backend.has("ours", key):
        backend.add(side, key)
        return
    if side == "theirs":
        backend.take_theirs(key)
    else:
        backend.take_ours(key)


# ---------------------------------------------------------------------------
# Comment-preserving set-value-at-path (post-conflict rebuild seam).
# ---------------------------------------------------------------------------


def set_at_path(model: object, path: str, value: object) -> None:
    """Set the leaf at dotted ``path`` in ``model`` to ``value`` in place.

    The seam the take-tracked disposition uses to write a chosen value back at
    a :class:`PathConflict`'s path after the 3-way merge. ``path`` is the same
    DOTTED grammar :attr:`PathConflict.path` uses (``a.b.c``); a list-suffix
    segment (``[*]`` / ``[]``) is rejected with :class:`ValueError` because this
    seam addresses mapping leaves only. ``value`` is a plain-python value — a
    scalar, ``list`` or ``dict`` (matching :attr:`PathConflict.theirs`, which is
    already unwrapped).

    Across all backends the write is comment-preserving:

    * ruamel ``CommentedMap`` round-trips, so sibling comments / anchors / quotes
      survive a plain assignment;
    * the json-five model splices the parent's stored ``.keys`` / ``.values`` in
      lockstep (never the derived ``key_value_pairs``) and, on REPLACING an
      existing leaf, carries that leaf's ``wsc_before`` forward — both via
      :func:`setforge.scalar_path._set_jsonc_leaf`;
    * a plain ``dict`` carries no comments, so assignment suffices.

    A missing intermediate PARENT raises :class:`KeyError` (no
    auto-vivification), matching the :mod:`setforge.scalar_path` semantics.
    A list-suffix segment raises :class:`ValueError`. When the resolved
    parent is not a mapping (so the leaf cannot be addressed by key),
    :class:`~setforge.errors.MergeTypeMismatch` propagates from the
    leaf-set step — callers wrapping this seam must account for it
    alongside ``KeyError`` / ``ValueError``.
    """
    if "[*]" in path or "[]" in path:
        raise ValueError(f"list suffix not allowed for set-at-path: {path!r}")
    segments = path.split(".")
    parent = _descend_set_parent(_json5_inner(model), segments, path)
    leaf = segments[-1]
    _set_leaf(parent, leaf, value, path)


def _descend_set_parent(node: object, segments: list[str], path: str) -> object:
    """Walk ``segments[:-1]`` and return the leaf's parent node.

    Raises :class:`KeyError` when any intermediate parent is missing (no
    auto-vivification), mirroring :func:`setforge.scalar_path` navigation.
    """
    for depth, seg in enumerate(segments[:-1]):
        child = _child_node(node, seg)
        if child is ABSENT:
            prefix = ".".join(segments[: depth + 1])
            raise KeyError(f"missing parent on path {path!r}: {prefix!r} is absent")
        node = child
    return node


def _child_node(node: object, key: str) -> object:
    """Return ``node``'s child at ``key`` or :data:`ABSENT` if missing."""
    if isinstance(node, JSONObject):
        idx = _find_key_index(node, key)
        return ABSENT if idx is None else node.values[idx]
    if isinstance(node, Mapping):
        return node.get(key, ABSENT)
    return ABSENT


def _set_leaf(parent: object, leaf: str, value: object, path: str) -> None:
    """Set ``parent[leaf]`` to ``value`` per the parent's backend.

    json-five parents go through :func:`setforge.scalar_path._set_jsonc_leaf`
    (keys/values spliced in lockstep, replaced-leaf ``wsc_before`` preserved);
    ruamel ``CommentedMap`` and plain ``dict`` take a plain assignment (ruamel's
    round-trip mode keeps sibling comments). A parent that is not a mapping is a
    shape error and raises :class:`~setforge.errors.MergeTypeMismatch`.
    """
    if isinstance(parent, JSONObject):
        _set_jsonc_leaf(parent, leaf, value)
        return
    if isinstance(parent, MutableMapping):
        parent[leaf] = value
        return
    raise MergeTypeMismatch(
        f"cannot set leaf at {path!r}: parent is {type(parent).__name__}, not a mapping"
    )
