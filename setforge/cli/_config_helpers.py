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

# ruamel.yaml ships py.typed without resolvable annotations.
from ruamel.yaml.comments import (  # type: ignore[import-not-found]
    CommentedMap,
    CommentedSeq,
)

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
    return (
        node_from_annotation(non_none[0]) if non_none else FieldNode(ann, False, (), {})
    )


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
    if not yaml_path.exists() or not yaml_path.read_text(encoding="utf-8").strip():
        return CommentedMap()
    data = yaml.load(yaml_path.read_text(encoding="utf-8"))
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
        existing.remove(value)
    else:
        # Scalar unset: pop the key (and its comment-association entry).
        del parent[leaf]
        parent.ca.items.pop(leaf, None)
    return doc


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
