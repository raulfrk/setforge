"""Read-at-path + comment-preserving single-scalar splice for YAML + JSONC.

This module is the navigate-and-write seam consumed by the forked-scalar
disposition: a force-merge of ONE user-specified key-path. It wraps the
existing path navigation in :mod:`setforge.yaml_merge` (ruamel round-trip
docs) and :mod:`setforge.jsonc` (json-five model docs) with two operations:

* ``read_scalar_*`` — return the scalar value at a path, ``None`` for a
  present ``null``, or :data:`setforge.scalar_merge.ABSENT` for an absent
  key, so the result feeds :func:`setforge.scalar_merge.resolve_scalar`
  directly.
* ``write_scalar_*`` — apply a :class:`~setforge.scalar_merge.ScalarResolution`
  to the leaf: ``TAKE`` sets-or-creates it, ``DELETE`` removes it, both
  comment-preserving and mutating the doc/model IN PLACE.

Contract notes:

* Only SINGLE scalars. A path whose tokenizer yields a list-suffix segment
  (``[*]`` / ``[]``) is rejected at parse with a clear :class:`ValueError`.
* NO auto-vivification of missing intermediate PARENTS — a write whose
  parent is absent raises :class:`KeyError` rather than building structure
  (hard-failing on missing paths is a later validate concern).
* The model/doc is mutated in place; a created JSONC leaf is appended to the
  stored ``.keys`` / ``.values`` lists (``key_value_pairs`` is a derived
  property, so appending to it would be lost on dump).
"""

import json
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from json5.model import (
    BooleanLiteral,
    DoubleQuotedString,
    Float,
    Integer,
    JSONObject,
    NullLiteral,
    TrailingComma,
)

from setforge.errors import MergeTypeMismatch
from setforge.jsonc import (
    PATH_SEPARATOR,
    _detect_indent,
    _find_key_index,
    _python_to_node,
    _require_top_object,
    _split_path,
    _walk_jsonobject_path,
)
from setforge.scalar_merge import ABSENT, ScalarOutcome, ScalarResolution
from setforge.yaml_merge import _MISSING, _navigate, _parse_path

_LIST_SUFFIX_KINDS: frozenset[str] = frozenset({"key_each", "key_whole"})


# ---------------------------------------------------------------------------
# YAML.
# ---------------------------------------------------------------------------


def _yaml_key_tokens(path: str) -> list[tuple[str, str]]:
    """Parse ``path`` into ``(kind, key)`` tokens, rejecting list suffixes.

    Reuses :func:`setforge.yaml_merge._parse_path` (which already forbids a
    ``[*]`` / ``[]`` suffix mid-path) and additionally rejects a TERMINAL
    list suffix — this seam handles single scalars only. Returns the parsed
    tokens so a caller can both navigate (via :func:`_navigate`) and read off
    plain key names without re-parsing.
    """
    tokens = _parse_path(path)
    for kind, _key in tokens:
        if kind in _LIST_SUFFIX_KINDS:
            raise ValueError(
                f"list suffix not allowed for single-scalar path: {path!r}"
            )
    return tokens


def read_scalar_yaml(doc: object, path: str) -> object:
    """Return the scalar at ``path`` in a ruamel YAML ``doc``.

    Returns the typed scalar value, ``None`` for a present ``null``, or
    :data:`setforge.scalar_merge.ABSENT` for an absent key (including a
    missing intermediate parent). Rejects list-suffix paths with
    :class:`ValueError`. A path terminating on a non-scalar (a
    ``CommentedMap`` / ``CommentedSeq`` leaf) is rejected with
    :class:`~setforge.errors.MergeTypeMismatch`, mirroring the JSONC seam so
    both formats enforce the single-scalar contract at this boundary.
    """
    tokens = _yaml_key_tokens(path)
    value = _navigate(doc, tokens)
    if value is _MISSING:
        return ABSENT
    if isinstance(value, Mapping | Sequence) and not isinstance(value, str | bytes):
        raise MergeTypeMismatch(
            f"path does not terminate on a scalar: leaf is {type(value).__name__}"
        )
    return value


def write_scalar_yaml(doc: object, path: str, resolution: ScalarResolution) -> None:
    """Apply ``resolution`` to the leaf at ``path`` in a ruamel YAML ``doc``.

    ``TAKE`` sets-or-creates the leaf (a created leaf appends to its parent
    in document order); ``DELETE`` removes it (a no-op when already absent).
    ``CONFLICT`` is not a writable outcome and raises :class:`ValueError`.
    Comments on siblings are preserved by ruamel's round-trip mode. Mutates
    ``doc`` in place; a missing intermediate PARENT raises :class:`KeyError`
    (no auto-vivification).
    """
    keys = [key for _kind, key in _yaml_key_tokens(path)]
    parent = _descend_yaml_parent(doc, keys, path)
    leaf = keys[-1]
    _apply_resolution_mapping(parent, leaf, resolution)


def _descend_yaml_parent(
    doc: object, keys: list[str], path: str
) -> MutableMapping[Any, Any]:
    """Walk ``doc`` through ``keys[:-1]`` and return the leaf's parent map.

    Raises :class:`KeyError` if any intermediate is missing (no vivify) and
    :class:`MergeTypeMismatch` if an intermediate exists but is not a map.
    """
    node: object = doc
    for depth, key in enumerate(keys[:-1]):
        if not isinstance(node, Mapping) or key not in node:
            prefix = ".".join(keys[: depth + 1])
            raise KeyError(f"missing parent on path {path!r}: {prefix!r} is absent")
        node = node[key]
    if not isinstance(node, MutableMapping):
        prefix = ".".join(keys[:-1]) or "<root>"
        raise MergeTypeMismatch(
            f"cannot set leaf at {path!r}: parent {prefix!r} is not a mapping"
        )
    return node


def _apply_resolution_mapping(
    parent: MutableMapping[Any, Any], leaf: str, resolution: ScalarResolution
) -> None:
    """Apply ``resolution`` to ``parent[leaf]`` (ruamel CommentedMap path)."""
    match resolution.outcome:
        case ScalarOutcome.TAKE:
            parent[leaf] = resolution.value
        case ScalarOutcome.DELETE:
            if leaf in parent:
                del parent[leaf]
        case ScalarOutcome.CONFLICT:
            raise ValueError("cannot write a CONFLICT resolution to a scalar leaf")


# ---------------------------------------------------------------------------
# JSONC.
# ---------------------------------------------------------------------------


def _jsonc_segments(path: str) -> list[str]:
    """Split a JSONC ``" > "`` path, rejecting list-suffix segments.

    Reuses :func:`setforge.jsonc._split_path`; this seam handles single
    scalars only, so a segment ending in ``[*]`` / ``[]`` is rejected with
    :class:`ValueError`.
    """
    segments = _split_path(path)
    for seg in segments:
        if seg.endswith("[*]") or seg.endswith("[]"):
            raise ValueError(
                f"list suffix not allowed for single-scalar path: {path!r}"
            )
    return segments


def read_scalar_jsonc(model: object, path: str) -> object:
    """Return the scalar at ``path`` in a json-five ``model``.

    Returns the typed scalar value, ``None`` for a present ``NullLiteral``,
    or :data:`setforge.scalar_merge.ABSENT` for an absent key (including a
    missing intermediate parent). :func:`setforge.jsonc._walk_jsonobject_path`
    returns ``None`` only for a missing parent, never a leaf — so a leaf
    fetched off its result cannot tell an absent key from a present ``null``.
    Here the leaf is looked up by index AFTER the walk, restoring that
    distinction: a present ``NullLiteral`` reads back as ``None`` while an
    absent key reads back as ``ABSENT``. Rejects list-suffix paths with
    :class:`ValueError`.
    """
    segments = _jsonc_segments(path)
    top = _require_top_object(model)
    parent = _walk_jsonobject_path(top, segments[:-1])
    if parent is None:
        return ABSENT
    idx = _find_key_index(parent, segments[-1])
    if idx is None:
        return ABSENT
    return _node_to_python(parent.values[idx])


def write_scalar_jsonc(model: object, path: str, resolution: ScalarResolution) -> None:
    """Apply ``resolution`` to the leaf at ``path`` in a json-five ``model``.

    ``TAKE`` sets-or-creates the leaf; a created leaf appends a fresh
    :class:`KeyValuePair`'s key/value nodes to the parent's stored ``.keys``
    / ``.values`` (NOT ``key_value_pairs``, a derived property) so it
    survives re-dump. ``DELETE`` removes the leaf's key/value pair (no-op
    when absent). ``CONFLICT`` raises :class:`ValueError`. Comments on
    siblings are preserved. Mutates ``model`` in place; a missing
    intermediate PARENT raises :class:`KeyError` (no auto-vivification).
    """
    segments = _jsonc_segments(path)
    top = _require_top_object(model)
    parent = _walk_jsonobject_path(top, segments[:-1])
    if parent is None:
        prefix = PATH_SEPARATOR.join(segments[:-1])
        raise KeyError(f"missing parent on path {path!r}: {prefix!r} is absent")
    leaf = segments[-1]
    match resolution.outcome:
        case ScalarOutcome.TAKE:
            _set_jsonc_leaf(parent, leaf, resolution.value)
        case ScalarOutcome.DELETE:
            _delete_jsonc_leaf(parent, leaf)
        case ScalarOutcome.CONFLICT:
            raise ValueError("cannot write a CONFLICT resolution to a scalar leaf")


def _member_indent(parent: JSONObject) -> str:
    """Return the per-member leading indent (newline + indent) for ``parent``.

    Reuses :func:`setforge.jsonc._detect_indent`, which scans existing keys'
    ``wsc_before`` for a newline-bearing prefix and falls back to ``"\\n  "``.
    """
    return _detect_indent(parent)


def _trailing_close_segment(parent: JSONObject) -> str:
    """Return the whitespace that precedes ``parent``'s closing ``}``.

    json-five stores the run between the final member and the ``}`` on the
    object's :class:`~json5.model.TrailingComma` ``wsc_after`` (when a
    trailing comma is present) — its last string element is the closing
    indent. Falls back to a bare ``"\\n"`` when no usable run is found.
    """
    tc = parent.trailing_comma
    after = list(getattr(tc, "wsc_after", None) or []) if tc is not None else []
    if after and isinstance(after[-1], str):
        return after[-1]
    return "\n"


def _set_jsonc_leaf(parent: JSONObject, leaf: str, value: object) -> None:
    """Set-or-create ``parent[leaf]`` to a fresh node for ``value``.

    Existing leaf: replace the value node in place, preserving its leading
    whitespace so a trailing comment on the same line survives.

    New leaf: append key + value nodes to the STORED ``.keys`` / ``.values``
    lists (``key_value_pairs`` is a derived property — appending there is
    lost on dump). The append also re-homes the prior last member's trailing
    comment: json-five parks that comment on the object's trailing-comma
    ``wsc_after``, so it must move onto the NEW key's ``wsc_before`` or it
    would migrate onto the appended member's line.
    """
    new_value = _python_to_node(value)
    idx = _find_key_index(parent, leaf)
    if idx is not None:
        existing = parent.values[idx]
        new_value.wsc_before = getattr(existing, "wsc_before", None) or [" "]
        parent.values[idx] = new_value
        return

    close_segment = _trailing_close_segment(parent)
    tc = parent.trailing_comma
    prior_trailing = (
        list(getattr(tc, "wsc_after", None) or [])[:-1] if tc is not None else []
    )

    new_value.wsc_before = [" "]
    new_key = DoubleQuotedString(characters=leaf, raw_value=json.dumps(leaf))
    # Prior last member's trailing comment (if any) + this member's indent.
    new_key.wsc_before = [*prior_trailing, _member_indent(parent)]
    parent.keys.append(new_key)
    parent.values.append(new_value)

    if tc is None:
        parent.trailing_comma = TrailingComma()
        tc = parent.trailing_comma
    tc.wsc_after = [close_segment]


def _delete_jsonc_leaf(parent: JSONObject, leaf: str) -> None:
    """Remove ``parent[leaf]`` from the stored key/value lists (no-op if absent).

    A member's OWN trailing comment lives on the NEXT member's key
    ``wsc_before`` (or the object's trailing-comma ``wsc_after`` for the last
    member). Deleting a member therefore must move the deleted key's
    ``wsc_before`` — which holds the PREVIOUS member's trailing comment —
    forward onto its successor, or the predecessor's comment is dropped with
    it.
    """
    idx = _find_key_index(parent, leaf)
    if idx is None:
        return
    deleted_before = getattr(parent.keys[idx], "wsc_before", None) or []
    is_last = idx == len(parent.keys) - 1
    del parent.keys[idx]
    del parent.values[idx]

    if is_last:
        if parent.keys:
            # The deleted member's wsc_before carries the new-last member's
            # trailing comment; re-home it before the closing brace via the
            # trailing comma, swapping the member indent for the close indent.
            close = _trailing_close_segment(parent)
            carried = deleted_before[:-1] if deleted_before else []
            tc = parent.trailing_comma
            if tc is None:
                parent.trailing_comma = TrailingComma()
                tc = parent.trailing_comma
            tc.wsc_after = [*carried, close]
        return
    # Non-last delete: hand the predecessor's trailing comment to the
    # successor whose key now follows the predecessor directly.
    successor = parent.keys[idx]
    successor.wsc_before = deleted_before


def _node_to_python(node: object) -> object:
    """Convert a json-five scalar leaf node to its Python value.

    Inverse of the scalar branch of :func:`setforge.jsonc._python_to_node`.
    A non-scalar leaf (``JSONObject`` / ``JSONArray`` / any other node) means
    the path did not terminate on a scalar; the single-scalar seam rejects it
    with :class:`~setforge.errors.MergeTypeMismatch`.
    """
    match node:
        case NullLiteral():
            return None
        case BooleanLiteral(value=v):
            return v
        case Integer(raw_value=raw):
            return int(raw, 0)
        case Float(raw_value=raw):
            return float(raw)
        case DoubleQuotedString(raw_value=raw):
            return json.loads(raw)
        case _:
            raise MergeTypeMismatch(
                f"path does not terminate on a scalar: leaf is {type(node).__name__}"
            )
