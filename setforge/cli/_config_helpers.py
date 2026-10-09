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
    holder = navigate(doc, parts[:-2]) if len(parts) > 1 else None
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
        if isinstance(existing, CommentedSeq):
            _remove_entry(parent, leaf, existing, existing.index(value))
        else:
            existing.remove(value)
    else:
        _remove_entry(holder, parts[-2] if holder is not None else None, parent, leaf)
    return doc


def _remove_entry(
    holder: CommentedMap | None,
    holder_key: str | None,
    node: Any,  # noqa: ANN401 — a CommentedMap or CommentedSeq
    key: Any,  # noqa: ANN401 — a mapping key or a list index
) -> None:
    """Delete ``node[key]``, keeping the comment and blank lines around it.

    ``node`` is ``holder[holder_key]`` (the document itself when ``holder`` is
    ``None``). The lines above the entry and the lines after it belong to its
    neighbours, wherever ruamel stored them; only the entry's own lines go.
    """
    kept = _comment_above(node, key) + _following_comment(node, key)
    position = list(node).index(key) if isinstance(node, CommentedMap) else key
    del node[key]
    if isinstance(node, CommentedMap):
        node.ca.items.pop(key, None)
    if not node:
        _keep_after_emptied(holder, holder_key, node, kept)
    elif kept and position:
        before = list(node)[position - 1] if isinstance(node, CommentedMap) else key - 1
        _keep_after(node, before, kept)
    elif kept:
        _keep_above(
            node, next(iter(node)) if isinstance(node, CommentedMap) else 0, kept
        )


def _keep_after_emptied(
    holder: CommentedMap | None,
    holder_key: str | None,
    node: Any,  # noqa: ANN401 — a CommentedMap or CommentedSeq
    kept: str,
) -> None:
    """Attach ``kept`` after the ``{}`` / ``[]`` the emptied ``node`` renders as."""
    node.fa.set_flow_style()
    kept += _lines(node.ca.end)
    node.ca.end.clear()
    if not kept:
        return
    key_entry = holder.ca.items.get(holder_key) if holder is not None else None
    if key_entry is not None and not key_entry[3]:
        # ruamel writes the key's comment in place of the collection's own.
        entry, slot = key_entry, 2
    else:
        if node.ca.comment is None:
            node.ca.comment = [None, None]
        entry, slot = node.ca.comment, 0
    if entry[slot] is None:
        entry[slot] = CommentToken("\n" + kept, CommentMark(0))
    else:
        entry[slot].value += kept


def _lines(tokens: list[CommentToken] | None) -> str:
    """The text of comment tokens ruamel keeps one per line, indentation included."""
    return "".join(
        " " * token.column + token.value if token.value.strip() else token.value
        for token in tokens or []
    )


def _following_comment(node: Any, key: Any) -> str:  # noqa: ANN401
    """Comment and blank lines after ``node[key]`` that belong to what follows.

    ruamel stores them in the same token as the end-of-line comment of the
    entry's last line (the last entry's, when the value is a block mapping or
    list), and after a one-line last entry (``- {x: 1}``) with no end-of-line
    comment, at the end of the list or mapping that entry is in.
    """
    chain = _last_chain(node, key)
    leaf, leaf_key = chain[-1]
    entry = leaf.ca.items.get(leaf_key)
    token = entry[_tail_slot(leaf)] if entry else None
    tail = token.value.split("\n", 1)[1] if token and "\n" in token.value else ""
    return tail + "".join(_lines(inner.ca.end) for inner, _ in reversed(chain[1:]))


def _comment_above(node: Any, key: Any) -> str:  # noqa: ANN401
    """Comment and blank lines above ``node[key]`` that ruamel stores with it.

    After a one-line list or mapping (``[a, b]``) with no end-of-line comment
    they are kept with the entry that follows instead of in the tail of the
    one before.
    """
    entry = node.ca.items.get(key)
    return _lines(entry[1]) if entry else ""


def _last_chain(node: Any, key: Any) -> list[tuple[Any, Any]]:  # noqa: ANN401
    """``(node, key)`` and each last entry below it, down to its last line.

    A block mapping or list ends on its last entry's line, so the comment after
    the whole value is stored with that entry, however deep.
    """
    chain = [(node, key)]
    value = node[key]
    while (
        isinstance(value, (CommentedMap, CommentedSeq))
        and value
        and not value.fa.flow_style()
    ):
        last = list(value)[-1] if isinstance(value, CommentedMap) else len(value) - 1
        chain.append((value, last))
        value = value[last]
    return chain


def _tail_slot(leaf: Any) -> int:  # noqa: ANN401
    """Which comment slot a mapping key (2) or a list item (0) keeps its tail in."""
    return 2 if isinstance(leaf, CommentedMap) else 0


def _keep_above(node: Any, key: Any, kept: str) -> None:  # noqa: ANN401
    """Attach ``kept`` above ``node[key]``, before what is stored there already."""
    entry = node.ca.items.setdefault(key, [None, None, None, None])
    entry[1] = [CommentToken(kept, CommentMark(0)), *(entry[1] or [])]


def _keep_after(node: Any, key: Any, kept: str) -> None:  # noqa: ANN401
    """Attach ``kept`` after everything stored after ``node[key]``."""
    chain = _last_chain(node, key)
    for inner, _ in chain[1:]:
        if inner.ca.end:
            inner.ca.end.append(CommentToken(kept, CommentMark(0)))
            return
    leaf, leaf_key = chain[-1]
    slot = _tail_slot(leaf)
    entry = leaf.ca.items.setdefault(leaf_key, [None, None, None, None])
    if entry[slot] is not None:
        entry[slot].value += kept
    elif not isinstance(leaf, CommentedSeq) or not isinstance(
        leaf[leaf_key], (CommentedMap, CommentedSeq)
    ):
        entry[slot] = CommentToken("\n" + kept, CommentMark(0))
    elif leaf_key + 1 < len(leaf):
        # Where ruamel itself keeps the lines after a one-line list entry: it
        # can join later lines when that entry carries them.
        _keep_above(leaf, leaf_key + 1, kept)
    else:
        if leaf.ca.comment is None:
            leaf.ca.comment = [None, None]
        leaf.ca.end.append(CommentToken(kept, CommentMark(0)))


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
