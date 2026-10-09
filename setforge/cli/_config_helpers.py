"""Helpers for setforge.cli.config — module-private.

Two subsystems lifted out of the 1000+ line monolithic
:mod:`setforge.cli.config` so the CLI module stays focused on typer
shims + per-verb orchestration:

- **Schema walk** — Pydantic ``model_fields`` introspection that drives
  list-vs-scalar dispatch + dotted-path completion. :class:`FieldNode`,
  :func:`walk_model`, the per-shape :func:`node_from_*` helpers,
  :func:`resolve_path`, :func:`enumerate_paths`.
- **YAML navigation** — round-trip CommentedMap walks for the in-memory
  mutate-then-write pipeline. :func:`load_doc`, :func:`navigate`,
  :func:`navigate_to_parent`, :func:`apply_add`, :func:`apply_remove`,
  :func:`to_plain`.

Mirrors the project pattern (install.py → _install_helpers.py;
plugins.py → _plugin_helpers.py). NO typer decorators here, NO
``app`` import — this module is internal-only and stays out of typer's
command surface.
"""

from __future__ import annotations

import types as _types
import typing as _typing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from ruamel.yaml.comments import (
    CommentedMap,
    CommentedSeq,
)
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.scalarint import OctalInt
from ruamel.yaml.tokens import CommentToken

from setforge.errors import SetforgeError
from setforge.migrations._yaml_ops import yaml_rt

__all__ = [
    "FieldNode",
    "apply_add",
    "apply_remove",
    "enumerate_paths",
    "is_dict_typed",
    "load_doc",
    "navigate",
    "navigate_to_parent",
    "node_from_annotation",
    "resolve_path",
    "scalar_leaf_from_dict_value",
    "to_plain",
    "walk_model",
]


# ---------------------------------------------------------------------------
# Schema walk
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class FieldNode:
    """One node in the dotted-path schema tree.

    ``annotation`` is the Pydantic-introspected type. ``is_list`` is the
    fast dispatch flag for list-vs-scalar. ``enum_values`` carries the
    closed-set values for ``StrEnum`` / ``Literal`` scalars (used by
    value completion). ``children`` is the next-level field dict for
    nested BaseModels; empty for leaf scalars and lists.
    """

    annotation: Any
    is_list: bool
    enum_values: tuple[str, ...]
    children: dict[str, FieldNode]


def walk_model(model: type[BaseModel]) -> dict[str, FieldNode]:
    """Walk ``model.model_fields`` recursively into a node tree."""
    out: dict[str, FieldNode] = {}
    for name, info in model.model_fields.items():
        out[name] = node_from_annotation(info.annotation)
    return out


def node_from_annotation(ann: Any) -> FieldNode:  # noqa: ANN401 — Pydantic annotations are dynamic
    """Build a ``FieldNode`` for one Pydantic field annotation.

    Dispatches by annotation shape: strips ``Annotated[T, ...]`` metadata
    first, then routes to a per-shape helper for Literal / Union / list /
    dict / BaseModel / StrEnum / bare-type.
    """
    if _typing.get_origin(ann) is _typing.Annotated or hasattr(ann, "__metadata__"):
        inner_args = _typing.get_args(ann)
        if inner_args:
            return node_from_annotation(inner_args[0])

    origin = getattr(ann, "__origin__", None)
    args = getattr(ann, "__args__", ())

    if origin is _typing.Literal:
        return _node_from_literal(ann, args)
    if isinstance(ann, _types.UnionType) or origin is _typing.Union:
        return _node_from_union(ann, args)
    if origin is list:
        return _node_from_list(ann)
    if origin is dict:
        return _node_from_dict(ann, args)
    return _node_from_base(ann)


def _node_from_literal(ann: Any, args: tuple[Any, ...]) -> FieldNode:  # noqa: ANN401
    """Pydantic discriminator fields render as ``Literal[Enum.MEMBER]``.

    Surface the literal values as ``enum_values`` so value-completion
    on a discriminator path (e.g. ``source.kind``) yields the literal
    options. Must run BEFORE the union check because ``Literal`` carries
    ``__args__`` too.
    """
    literal_values = tuple(
        (a.value if isinstance(a, StrEnum) else str(a)) for a in args
    )
    return FieldNode(
        annotation=ann, is_list=False, enum_values=literal_values, children={}
    )


def _node_from_union(ann: Any, args: tuple[Any, ...]) -> FieldNode:  # noqa: ANN401
    """PEP 604 / typing.Union shape (``X | None`` / ``X | Y`` / ``Optional[X]``).

    Single non-None arm collapses to ``Optional[X]`` and recurses. Multi-arm
    unions merge children across arms so dotted paths into either member
    resolve via the same dispatch. For discriminator fields (one
    ``Literal[...]`` per arm) ``enum_values`` accumulates across arms so
    completion on the union surfaces every arm's discriminator value.
    """
    non_none = [a for a in args if a is not type(None)]
    if len(non_none) == 1:
        return node_from_annotation(non_none[0])
    merged_children: dict[str, FieldNode] = {}
    for arm in non_none:
        arm_node = node_from_annotation(arm)
        for k, v in arm_node.children.items():
            if k in merged_children and v.enum_values:
                existing = merged_children[k]
                combined = tuple(dict.fromkeys((*existing.enum_values, *v.enum_values)))
                merged_children[k] = FieldNode(
                    annotation=existing.annotation,
                    is_list=existing.is_list,
                    enum_values=combined,
                    children=existing.children,
                )
            else:
                merged_children.setdefault(k, v)
    if merged_children:
        return FieldNode(
            annotation=ann, is_list=False, enum_values=(), children=merged_children
        )
    # ``non_none`` is non-empty here: an all-``None`` union (``None | None``)
    # is not a valid annotation, and the single-arm case returned above.
    assert non_none, "union must contain at least one non-None arm"
    return node_from_annotation(non_none[0])


def _node_from_list(ann: Any) -> FieldNode:  # noqa: ANN401
    """``list[T]`` → is_list=True; children empty (list elements are values)."""
    return FieldNode(annotation=ann, is_list=True, enum_values=(), children={})


def _node_from_dict(ann: Any, args: tuple[Any, ...]) -> FieldNode:  # noqa: ANN401
    """``dict[K, V]`` — expose value-model children for ``dict[str, BaseModel]``."""
    if len(args) == 2 and isinstance(args[1], type) and issubclass(args[1], BaseModel):
        return FieldNode(
            annotation=ann,
            is_list=False,
            enum_values=(),
            children=walk_model(args[1]),
        )
    return FieldNode(annotation=ann, is_list=False, enum_values=(), children={})


def _node_from_base(ann: Any) -> FieldNode:  # noqa: ANN401
    """Bare-type fallback: BaseModel subclass / StrEnum / scalar."""
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return FieldNode(
            annotation=ann, is_list=False, enum_values=(), children=walk_model(ann)
        )
    if isinstance(ann, type) and issubclass(ann, StrEnum):
        return FieldNode(
            annotation=ann,
            is_list=False,
            enum_values=tuple(m.value for m in ann),
            children={},
        )
    return FieldNode(annotation=ann, is_list=False, enum_values=(), children={})


def is_dict_typed(node: FieldNode) -> bool:
    """True iff this node's annotation is ``dict[...]``.

    Covers ``dict[str, T]`` and ``dict[str, BaseModel]`` uniformly.
    """
    origin = getattr(node.annotation, "__origin__", None)
    return origin is dict


def resolve_path(schema: dict[str, FieldNode], dotted: str) -> FieldNode | None:
    """Resolve a dotted path against a pre-walked schema tree.

    Returns ``None`` if the path doesn't resolve (e.g. typo). Dict-value
    segments resolve through the dict's value-model children (so
    ``profiles.<name>.tracked_files`` reaches into ``Profile``). For
    plain ``dict[str, T]`` (no BaseModel value), a one-segment dive
    yields a scalar leaf typed as ``T``.
    """
    parts = dotted.split(".")
    current_tree = schema
    node: FieldNode | None = None
    i = 0
    while i < len(parts):
        part = parts[i]
        if part in current_tree:
            node = current_tree[part]
            current_tree = node.children
            i += 1
            continue
        # Not a known field — if the parent node is dict-typed, this
        # segment is a dict key. Two shapes:
        #   - dict[str, BaseModel]: parent's children were already
        #     populated; current_tree is the value-model's children →
        #     re-resolve `part` against that tree (so the inner field
        #     `tracked_files` resolves under `profiles.<name>`).
        #   - dict[str, T] (T scalar / list): no children → the path
        #     ends here. Return the parent node as the leaf.
        if node is not None and is_dict_typed(node):
            if node.children:
                current_tree = node.children
                i += 1
                continue
            return scalar_leaf_from_dict_value(node)
        return None
    return node


def scalar_leaf_from_dict_value(parent: FieldNode) -> FieldNode:
    """Synthesize a leaf ``FieldNode`` for a ``dict[str, T]`` value lookup."""
    # mypy infers ``getattr(..., ())`` as ``tuple[()]`` (the literal-empty-tuple
    # default), so the ``args[1]`` access below is flagged "Tuple index out of
    # range" even though the runtime guard ``len(args) == 2`` makes the access
    # safe. Cast to ``tuple[Any, ...]`` so the runtime-guarded index is well-
    # typed.
    args = _typing.cast(tuple[Any, ...], getattr(parent.annotation, "__args__", ()))
    if len(args) == 2:
        return node_from_annotation(args[1])
    return FieldNode(
        annotation=parent.annotation, is_list=False, enum_values=(), children={}
    )


def enumerate_paths(schema: dict[str, FieldNode]) -> list[str]:
    """Yield every concrete dotted path under a pre-walked schema tree."""
    out: list[str] = []
    _walk_paths(schema, "", out)
    return out


def _walk_paths(tree: dict[str, FieldNode], prefix: str, out: list[str]) -> None:
    """Recursive helper for :func:`enumerate_paths`."""
    for name, node in tree.items():
        path = f"{prefix}.{name}" if prefix else name
        out.append(path)
        if node.children:
            _walk_paths(node.children, path, out)


# ---------------------------------------------------------------------------
# YAML doc navigation (mutate-in-place CommentedMap / CommentedSeq)
# ---------------------------------------------------------------------------


def load_doc(yaml_path: Path) -> CommentedMap:
    """Round-trip parse ``yaml_path``; return an empty map if absent."""
    yaml = yaml_rt()
    if not yaml_path.exists():
        return CommentedMap()
    try:
        text = yaml_path.read_text(encoding="utf-8")
        data = yaml.load(text) if text.strip() else None
    except (YAMLError, UnicodeDecodeError) as exc:
        raise SetforgeError(f"malformed YAML in {yaml_path}: {exc}") from exc
    except OSError as exc:
        raise SetforgeError(f"cannot read {yaml_path}: {exc.strerror or exc}") from exc
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        raise SetforgeError(f"top-level of {yaml_path} must be a mapping")
    return data


def navigate(doc: CommentedMap, parts: list[str]) -> Any:  # noqa: ANN401 — YAML doc dynamic
    """Walk dotted path through doc; auto-create missing CommentedMap nodes."""
    current: Any = doc
    for part in parts:
        if not isinstance(current, CommentedMap):
            raise SetforgeError(f"cannot navigate into non-mapping at {part!r}")
        if part not in current:
            current[part] = CommentedMap()
        current = current[part]
    return current


def navigate_to_parent(doc: CommentedMap, dotted: str) -> tuple[Any, str]:
    """Return (parent_container, leaf_key) for the dotted path.

    Auto-creates intermediate CommentedMap nodes so a first-time
    mutation against a previously-absent path lands cleanly.
    """
    parts = dotted.split(".")
    if len(parts) == 1:
        return doc, parts[0]
    parent = navigate(doc, parts[:-1])
    return parent, parts[-1]


def apply_add(
    doc: CommentedMap, dotted: str, value: str, *, is_list: bool
) -> CommentedMap:
    """Apply an ``add`` mutation to ``doc`` in place; return the same doc."""
    parent, leaf = navigate_to_parent(doc, dotted)
    if not isinstance(parent, CommentedMap):
        raise SetforgeError(f"parent of {dotted!r} is not a mapping")
    if is_list:
        existing = parent.get(leaf)
        if existing is None:
            parent[leaf] = CommentedSeq()
            existing = parent[leaf]
        if not isinstance(existing, (list, CommentedSeq)):
            raise SetforgeError(f"{dotted!r} is a scalar, not a list — cannot append")
        if value in existing:
            raise SetforgeError(f"{dotted!r} already contains {value!r}")
        existing.append(value)
    elif dotted == "version":
        parent[leaf] = 1 if value == "1" else value
    elif (
        dotted.startswith("tracked_files.")
        and dotted.endswith(".mode")
        and len(dotted.split(".")) == 3
    ):
        try:
            mode = yaml_rt().load(value)
        except YAMLError as exc:
            raise SetforgeError(
                f"{dotted}: mode must be a YAML octal integer, e.g. 0o755"
            ) from exc
        if not isinstance(mode, OctalInt):
            raise SetforgeError(
                f"{dotted}: mode must be a YAML octal integer, e.g. 0o755"
            )
        parent[leaf] = mode
    else:
        parent[leaf] = value
    return doc


def apply_remove(
    doc: CommentedMap, dotted: str, value: str | None, *, is_list: bool
) -> CommentedMap:
    """Apply a ``remove`` mutation to ``doc`` in place; return the same doc."""
    parts = dotted.split(".")
    if len(parts) == 1:
        parent: Any = doc
        leaf = parts[0]
    else:
        parent = navigate(doc, parts[:-1])
        leaf = parts[-1]
    if not isinstance(parent, CommentedMap):
        raise SetforgeError(f"parent of {dotted!r} is not a mapping")
    if leaf not in parent:
        raise SetforgeError(f"{dotted!r} not present in YAML")
    if is_list:
        if value is None:
            raise SetforgeError(f"remove from list {dotted!r} requires <value>")
        existing = parent[leaf]
        if not isinstance(existing, (list, CommentedSeq)):
            raise SetforgeError(
                f"{dotted!r} is a scalar, not a list — cannot remove value"
            )
        if value not in existing:
            raise SetforgeError(f"{value!r} not in {dotted!r}")
        index = existing.index(value)
        tail = _entry_tail(existing, index)
        del existing[index]
        if tail:
            _keep_entry_tail(parent, leaf, index, tail)
    else:
        _unset_key(parent, leaf)
    return doc


def _unset_key(parent: CommentedMap, leaf: str) -> None:
    """Pop ``leaf`` (and its comment-association entry), keeping what follows."""
    tail = _following_comment(parent, leaf)
    keys = list(parent)
    index = keys.index(leaf)
    if index:
        tail = _comment_above(parent, leaf) + tail
    del parent[leaf]
    parent.ca.items.pop(leaf, None)
    if tail:
        _keep_following_comment(parent, keys, index, tail)


def _entry_tail(seq: list[Any], index: int) -> str:
    """Comment and blank lines after list entry ``index``'s own line.

    Like :func:`_following_comment`: ruamel keeps them in the same token as the
    entry's end-of-line comment, but they belong to what follows the entry.
    """
    if not isinstance(seq, CommentedSeq):
        return ""
    entry = seq.ca.items.get(index)
    token = entry[0] if entry else None
    if token is None or "\n" not in token.value:
        return ""
    return token.value.split("\n", 1)[1]


def _keep_entry_tail(parent: CommentedMap, leaf: str, index: int, tail: str) -> None:
    """Re-attach ``tail`` of the removed entry ``index`` of ``parent[leaf]``.

    It goes after the entry before it, after the key when the removed entry was
    the first of several, or after the ``[]`` the list now renders as.
    """
    seq = parent[leaf]
    if index:
        entry = seq.ca.items.setdefault(index - 1, [None, None, None, None])
        slot = 0
    elif seq:
        entry = parent.ca.items.setdefault(leaf, [None, None, None, None])
        slot = 2
    else:
        seq.fa.set_flow_style()
        key_entry = parent.ca.items.get(leaf)
        if key_entry is not None and not key_entry[3]:
            # ruamel writes the key's comment in place of the list's own.
            entry, slot = key_entry, 2
        else:
            if seq.ca.comment is None:
                seq.ca.comment = [None, None]
            entry, slot = seq.ca.comment, 0
    if entry[slot] is None:
        entry[slot] = CommentToken("\n" + tail, CommentMark(0))
    else:
        entry[slot].value += tail


def _following_comment(node: CommentedMap, key: str) -> str:
    """Comment and blank lines after ``key``'s line that belong to what follows.

    ruamel stores them in the same token as the end-of-line comment of the
    key's last line (the last entry's, when the value is a block mapping or list).
    """
    leaf, leaf_key = _last_leaf(node, key)
    entry = leaf.ca.items.get(leaf_key)
    token = entry[_tail_slot(leaf)] if entry else None
    if token is None or "\n" not in token.value:
        return ""
    return token.value.split("\n", 1)[1]


def _comment_above(node: CommentedMap, key: str) -> str:
    """Comment and blank lines above ``key`` that ruamel stores with the key.

    After a flow list (``[a, b]``) with no end-of-line comment they are kept
    with the key that follows instead of in the tail of the one before.
    """
    entry = node.ca.items.get(key)
    tokens = entry[1] if entry and entry[1] else []
    return "".join(
        " " * token.column + token.value if token.value.strip() else token.value
        for token in tokens
    )


def _last_leaf(node: Any, key: Any) -> tuple[Any, Any]:  # noqa: ANN401
    """The collection and key (or index) whose comment follows ``node[key]``.

    A block mapping or list ends on its last entry's line, so the comment after
    the whole value is stored with that entry, however deep.
    """
    value = node[key]
    if not isinstance(value, (CommentedMap, CommentedSeq)):
        return node, key
    if not value or value.fa.flow_style():
        return node, key
    last = list(value)[-1] if isinstance(value, CommentedMap) else len(value) - 1
    return _last_leaf(value, last)


def _tail_slot(leaf: Any) -> int:  # noqa: ANN401
    """Which comment slot a mapping key (2) or a list item (0) keeps its tail in."""
    return 2 if isinstance(leaf, CommentedMap) else 0


def _keep_following_comment(
    node: CommentedMap, keys: list[str], index: int, tail: str
) -> None:
    """Re-attach ``tail`` of the removed ``keys[index]`` to its neighbour."""
    if index > 0:
        leaf, leaf_key = _last_leaf(node, keys[index - 1])
        slot = _tail_slot(leaf)
        entry = leaf.ca.items.setdefault(leaf_key, [None, None, None, None])
        if entry[slot] is None:
            entry[slot] = CommentToken("\n" + tail, CommentMark(0))
        else:
            entry[slot].value += tail
    elif index + 1 < len(keys):
        following = keys[index + 1]
        lines = [line.strip().removeprefix("#").strip() for line in tail.split("\n")]
        node.yaml_set_comment_before_after_key(
            following,
            before="\n".join(lines).strip("\n"),
            indent=node.lc.data[following][1],
        )


def to_plain(obj: Any) -> Any:  # noqa: ANN401 — recursive YAML coercion
    """Recursively convert a ruamel.yaml round-trip tree to plain dict/list.

    ``CommentedMap`` / ``CommentedSeq`` are subclasses of ``dict`` /
    ``list`` so the dedicated branches run BEFORE the dict / list
    fallbacks would catch them — kept explicit for readability.
    """
    if isinstance(obj, CommentedMap):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, CommentedSeq):
        return [to_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_plain(v) for v in obj]
    return obj
