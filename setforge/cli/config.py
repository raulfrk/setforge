"""``setforge config`` subcommand group — granular config CRUD (setforge-7dav).

Three verbs operate on either the host-local ``~/.config/setforge/local.yaml``
(``--local`` scope) or the tracked ``setforge.yaml`` (``--tracked`` scope):

- ``setforge config show`` — render the resolved YAML (or a dotted-path slice).
- ``setforge config add`` — append-to-list or set-scalar at a dotted path.
- ``setforge config remove`` — pop-from-list or unset-scalar at a dotted path.

Mutations parse the current YAML in round-trip mode, apply the dotted-path
change in-memory, run the appropriate schema validation against the candidate
document, diff-preview the change, then atomic-write via
:func:`setforge.migrations._yaml_ops.atomic_write_yaml`. Comment + key-order
+ whitespace preservation is non-negotiable.

List-vs-scalar dispatch comes from Pydantic ``model_fields`` introspection,
never a user flag. Shell tab-completion on both the ``<dotted-path>`` and
``<value>`` arguments dispatches off the same schema walk.
"""

from __future__ import annotations

import difflib
import functools
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.panel import Panel

# ruamel.yaml ships py.typed without resolvable annotations.
from ruamel.yaml.comments import (  # type: ignore[import-not-found]
    CommentedMap,
    CommentedSeq,
)

from setforge.binaries import LOCAL_CONFIG_PATH, ensure_local_config_stub
from setforge.cli import _TYPER_KWARGS, app
from setforge.cli._git_check import run_git_check_or_raise
from setforge.cli.validate import _LocalConfig
from setforge.config import Config, MarketplaceSource, MarketplaceSourceKind
from setforge.errors import ConfirmRequiresInteractive, SetforgeError
from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt
from setforge.source import get_resolved_source, validate_source_dir

# ``prompt_toolkit`` symbols are resolved lazily via PEP 562 so
# ``setforge config --help`` and the shell-completion callbacks stay
# fast (the prompt_toolkit cold-start is ~140 ms). Tests monkeypatch
# attributes on this module to intercept the dialogs.


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    if name == "input_dialog":
        from prompt_toolkit.shortcuts import input_dialog

        return input_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ConfigScope",
    "config_app",
]


class ConfigScope(StrEnum):
    """Which YAML file a ``setforge config`` mutation targets."""

    LOCAL = "local"
    TRACKED = "tracked"
    EFFECTIVE = "effective"


config_app: typer.Typer = typer.Typer(
    help="Granular CRUD over setforge.yaml / local.yaml.",
    no_args_is_help=True,
    **_TYPER_KWARGS,
)
app.add_typer(config_app, name="config")


# ---------------------------------------------------------------------------
# Scope resolution + tracked-config path discovery
# ---------------------------------------------------------------------------


def _resolve_scope(
    *,
    local: bool,
    tracked: bool,
    effective: bool = False,
) -> ConfigScope:
    """Enforce the ``--local`` / ``--tracked`` / ``--effective`` mutex.

    Exactly-one-required (no implicit default) raises
    :class:`typer.BadParameter` so the user sees a typer-formatted
    error rather than a stack trace. ``--effective`` is only valid on
    ``show``; callers pass ``effective=False`` for add/remove.
    """
    picked = [
        flag
        for flag, val in [
            ("--local", local),
            ("--tracked", tracked),
            ("--effective", effective),
        ]
        if val
    ]
    if not picked:
        raise typer.BadParameter(
            "exactly one of --local / --tracked"
            + ("/ --effective" if effective is False else "")
            + " is required"
        )
    if len(picked) > 1:
        joined = " + ".join(picked)
        raise typer.BadParameter(
            f"--local / --tracked / --effective are mutually exclusive; got {joined}"
        )
    if local:
        return ConfigScope.LOCAL
    if tracked:
        return ConfigScope.TRACKED
    return ConfigScope.EFFECTIVE


def _tracked_yaml_path() -> Path:
    """Return the tracked ``setforge.yaml`` resolved via the source layer."""
    resolved_source = get_resolved_source()
    source_dir = validate_source_dir(resolved_source)
    return source_dir / "setforge.yaml"


def _scope_yaml_path(scope: ConfigScope) -> Path:
    """Return the on-disk YAML file for ``scope`` (local | tracked)."""
    if scope is ConfigScope.LOCAL:
        return LOCAL_CONFIG_PATH
    if scope is ConfigScope.TRACKED:
        return _tracked_yaml_path()
    raise SetforgeError(f"_scope_yaml_path: unexpected scope {scope!r}")


# ---------------------------------------------------------------------------
# Schema walk (cached) — used by dispatch + completion
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _FieldNode:
    """One node in the dotted-path schema tree.

    ``annotation`` is the Pydantic-introspected type. ``is_list`` is the
    fast dispatch flag for list-vs-scalar. ``enum_values`` carries the
    closed-set values for ``StrEnum``-typed scalars (used by value
    completion). ``children`` is the next-level field dict for nested
    BaseModels; empty for leaf scalars and lists.
    """

    annotation: Any
    is_list: bool
    enum_values: tuple[str, ...]
    children: dict[str, _FieldNode]


def _walk_model(model: type[BaseModel]) -> dict[str, _FieldNode]:
    """Walk ``model.model_fields`` recursively into a node tree."""
    out: dict[str, _FieldNode] = {}
    for name, info in model.model_fields.items():
        out[name] = _node_from_annotation(info.annotation)
    return out


def _node_from_annotation(ann: Any) -> _FieldNode:  # noqa: ANN401, C901 — Pydantic annotations are dynamic
    """Build a ``_FieldNode`` for one Pydantic field annotation.

    Recurses into nested BaseModel annotations (``children`` populated)
    and unwraps ``X | None`` to the non-``None`` arm. ``list[T]`` /
    ``dict[K, V]`` get ``is_list=True`` (for ``list``) and
    ``children={}``; downstream sub-paths through dict values resolve
    at completion time against the live config state.
    """
    # Unwrap Optional / X | None
    origin = getattr(ann, "__origin__", None)
    args = getattr(ann, "__args__", ())
    if origin is None:
        # Bare type: BaseModel subclass, StrEnum, or scalar.
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            return _FieldNode(
                annotation=ann, is_list=False, enum_values=(), children=_walk_model(ann)
            )
        if isinstance(ann, type) and issubclass(ann, StrEnum):
            return _FieldNode(
                annotation=ann,
                is_list=False,
                enum_values=tuple(m.value for m in ann),
                children={},
            )
        return _FieldNode(annotation=ann, is_list=False, enum_values=(), children={})
    # Unions (X | None / X | Y).
    import types

    if (
        isinstance(ann, types.UnionType)
        or origin is type(None)
        or (origin is None and len(args) > 1)
    ):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _node_from_annotation(non_none[0])
        # Multi-arm union — pick first BaseModel arm for completion;
        # falls back to first non-None arg.
        for arm in non_none:
            if isinstance(arm, type) and issubclass(arm, BaseModel):
                return _node_from_annotation(arm)
        return (
            _node_from_annotation(non_none[0])
            if non_none
            else _FieldNode(ann, False, (), {})
        )
    # list[T] → is_list=True; children empty (list elements are values).
    if origin in (list,):
        return _FieldNode(annotation=ann, is_list=True, enum_values=(), children={})
    # dict[K, V] → children are dynamic (resolve at runtime by key);
    # value type recursion is dropped — completion handles dicts as
    # opaque mappings, mutate path indexes by key.
    if origin in (dict,):
        # For dict[str, BaseModel], expose the value model's children
        # so dotted-path "<dictkey>.subfield" still type-checks.
        if (
            len(args) == 2
            and isinstance(args[1], type)
            and issubclass(args[1], BaseModel)
        ):
            return _FieldNode(
                annotation=ann,
                is_list=False,
                enum_values=(),
                children=_walk_model(args[1]),
            )
        return _FieldNode(annotation=ann, is_list=False, enum_values=(), children={})
    return _FieldNode(annotation=ann, is_list=False, enum_values=(), children={})


@functools.cache
def _schema_local() -> dict[str, _FieldNode]:
    """Cached walk of the local-scope model schema."""
    # Use _LocalConfig (the validate-only union of source/binaries/claude).
    return _walk_model(_LocalConfig)


@functools.cache
def _schema_tracked() -> dict[str, _FieldNode]:
    """Cached walk of the tracked-scope ``Config`` model schema."""
    return _walk_model(Config)


def _resolve_path(scope: ConfigScope, dotted: str) -> _FieldNode | None:
    """Resolve a dotted path against the cached schema tree.

    Returns ``None`` if the path doesn't resolve (e.g. typo). Dict-value
    segments resolve through the dict's value-model children (so
    ``profiles.<name>.tracked_files`` reaches into ``Profile``).
    """
    schema = _schema_local() if scope is ConfigScope.LOCAL else _schema_tracked()
    parts = dotted.split(".")
    current = schema
    node: _FieldNode | None = None
    for part in parts:
        if part not in current:
            # Try dict-key wildcard: if the parent had children, the
            # part is a dict-key; node stays the value-model node.
            if node is not None and node.children:
                current = node.children
                continue
            return None
        node = current[part]
        current = node.children
    return node


def _enumerate_paths(scope: ConfigScope, prefix: str = "") -> list[str]:
    """Yield every concrete dotted path under ``scope``'s schema."""
    schema = _schema_local() if scope is ConfigScope.LOCAL else _schema_tracked()
    out: list[str] = []
    _walk_paths(schema, prefix, out)
    return out


def _walk_paths(tree: dict[str, _FieldNode], prefix: str, out: list[str]) -> None:
    """Recursive helper for ``_enumerate_paths``."""
    for name, node in tree.items():
        path = f"{prefix}.{name}" if prefix else name
        out.append(path)
        if node.children:
            _walk_paths(node.children, path, out)


# ---------------------------------------------------------------------------
# YAML doc navigation (mutate-in-place CommentedMap / CommentedSeq)
# ---------------------------------------------------------------------------


def _load_doc(yaml_path: Path) -> CommentedMap:
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


def _navigate(doc: CommentedMap, parts: list[str]) -> Any:  # noqa: ANN401 — YAML doc is dynamically shaped
    """Walk dotted path through doc; auto-create missing CommentedMap nodes."""
    current: Any = doc
    for part in parts:
        if not isinstance(current, CommentedMap):
            raise SetforgeError(f"cannot navigate into non-mapping at {part!r}")
        if part not in current:
            current[part] = CommentedMap()
        current = current[part]
    return current


def _navigate_to_parent(doc: CommentedMap, dotted: str) -> tuple[Any, str]:
    """Return (parent_container, leaf_key) for the dotted path.

    Auto-creates intermediate CommentedMap nodes so a first-time
    mutation against a previously-absent path lands cleanly.
    """
    parts = dotted.split(".")
    if len(parts) == 1:
        return doc, parts[0]
    parent = _navigate(doc, parts[:-1])
    return parent, parts[-1]


# ---------------------------------------------------------------------------
# Mutation primitives
# ---------------------------------------------------------------------------


def _apply_add(
    doc: CommentedMap, dotted: str, value: str, *, is_list: bool
) -> CommentedMap:
    """Apply an ``add`` mutation to ``doc`` in place; return the same doc."""
    parent, leaf = _navigate_to_parent(doc, dotted)
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


def _apply_remove(
    doc: CommentedMap, dotted: str, value: str | None, *, is_list: bool
) -> CommentedMap:
    """Apply a ``remove`` mutation to ``doc`` in place; return the same doc."""
    parts = dotted.split(".")
    if len(parts) == 1:
        parent: Any = doc
        leaf = parts[0]
    else:
        parent = _navigate(doc, parts[:-1])
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


# ---------------------------------------------------------------------------
# Validation (in-memory candidate doc)
# ---------------------------------------------------------------------------


def _validate_candidate(scope: ConfigScope, doc: CommentedMap) -> None:
    """Run the appropriate schema check against the in-memory candidate.

    Raises :class:`SetforgeError` on validation failure with a
    user-facing message. Per anti-smell #7, this fires BEFORE the
    atomic write so an invalid candidate never lands on disk.
    """
    plain = _to_plain(doc)
    if scope is ConfigScope.LOCAL:
        try:
            _LocalConfig.model_validate(plain)
        except ValidationError as exc:
            raise SetforgeError(
                f"local.yaml candidate failed validation:\n{exc}"
            ) from exc
    elif scope is ConfigScope.TRACKED:
        try:
            Config.model_validate(plain)
        except ValidationError as exc:
            raise SetforgeError(
                f"setforge.yaml candidate failed validation:\n{exc}"
            ) from exc


def _to_plain(obj: Any) -> Any:  # noqa: ANN401 — recursive YAML coercion
    """Recursively convert a ruamel.yaml round-trip tree to plain dict/list."""
    if isinstance(obj, CommentedMap):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, CommentedSeq):
        return [_to_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Diff preview + confirm panel
# ---------------------------------------------------------------------------


def _dump_to_str(doc: CommentedMap) -> str:
    """Serialize ``doc`` back to a string using the rt YAML factory."""
    import io

    yaml = yaml_rt()
    buf = io.StringIO()
    yaml.dump(doc, buf)
    return buf.getvalue()


def _render_diff(before: str, after: str, yaml_path: Path) -> str:
    """Build a unified diff with explicit ``fromfile=`` / ``tofile=`` labels."""
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{yaml_path}.before",
        tofile=f"{yaml_path}.after",
    )
    return "".join(diff_lines)


def _prompt_confirm(
    *, yaml_path: Path, diff_text: str, console: Console, yes: bool
) -> bool:
    """Render the diff panel + arrow-key prompt; return True to write.

    Short-circuits to True if ``yes`` is set. Raises
    :class:`ConfirmRequiresInteractive` on non-TTY stdin (mutate-gate
    posture per ``feedback_mutate_gate_vs_failure_prompt``).
    """
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            f"setforge config mutate of {yaml_path} requires --yes when stdin "
            f"is not a TTY"
        )
    console.print(Panel.fit(f"About to update [cyan]{yaml_path}[/cyan]:"))
    console.print(diff_text or "(no diff)")
    console.print("[green]Validate result: ✓ clean.[/green]")
    from setforge.cli import config as _self  # local alias for monkeypatch path

    choice = _self.radiolist_dialog(
        title="setforge config",
        text="Apply the mutation above?",
        values=[
            (False, "abort (no change)"),
            (True, "write"),
        ],
        default=False,
    ).run()
    if choice is None or choice is False:
        console.print("[red]✗ aborted[/red] — file not modified")
        return False
    console.print("[green]✓ writing[/green]")
    return True


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _slice_doc(doc: CommentedMap, dotted: str | None) -> Any:  # noqa: ANN401 — dynamic slice
    """Return the sub-tree at ``dotted`` or the full doc when ``None``."""
    if dotted is None:
        return doc
    parts = dotted.split(".")
    current: Any = doc
    for part in parts:
        if isinstance(current, CommentedMap):
            if part not in current:
                raise SetforgeError(f"path not present in YAML: {dotted!r}")
            current = current[part]
        else:
            raise SetforgeError(f"cannot traverse into non-mapping at {part!r}")
    return current


@config_app.command("show")
def config_show(
    path: str | None = typer.Argument(
        None,
        help="Optional dotted-path to scope output (e.g. plugins.add).",
        autocompletion=lambda ctx, incomplete: _complete_path_dispatch(ctx, incomplete),
    ),
    local: bool = typer.Option(
        False, "--local", help="Read ~/.config/setforge/local.yaml."
    ),
    tracked: bool = typer.Option(
        False, "--tracked", help="Read the tracked setforge.yaml."
    ),
    effective: bool = typer.Option(
        False, "--effective", help="Show the merged profile chain + overlay."
    ),
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="Required for profile-scoped paths."
    ),
) -> None:
    """Render the resolved YAML (or a dotted-path slice).

    With ``--local`` / ``--tracked``: prints the file's content (or a
    slice). With ``--effective``: prints the merged profile chain
    snapshot via the existing profile-show pathway.
    """
    scope = _resolve_scope(local=local, tracked=tracked, effective=effective)
    if scope is ConfigScope.EFFECTIVE:
        if profile is None:
            raise typer.BadParameter("--effective requires --profile=NAME")
        _show_effective(profile)
        return
    yaml_path = _scope_yaml_path(scope)
    doc = _load_doc(yaml_path)
    sliced = _slice_doc(doc, path)
    import io

    yaml = yaml_rt()
    buf = io.StringIO()
    yaml.dump(sliced, buf)
    text = buf.getvalue()
    typer.echo(text.rstrip())


def _show_effective(profile: str) -> None:
    """Print the merged profile chain via the existing ``profile show`` body."""
    from setforge.cli.profile import profile_show

    cfg_path = _tracked_yaml_path()
    profile_show(name=profile, config=cfg_path)


# ---------------------------------------------------------------------------
# add / remove (the mutating verbs)
# ---------------------------------------------------------------------------


def _check_profile_arg(dotted: str, profile: str | None) -> None:
    """Enforce ``--profile`` required-for-profiles + rejected-for-top-level rule."""
    needs_profile = dotted.startswith("profiles.")
    if needs_profile and profile is None:
        raise typer.BadParameter(
            f"path {dotted!r} is profile-scoped; pass --profile=NAME"
        )
    if not needs_profile and profile is not None:
        raise typer.BadParameter(
            f"path {dotted!r} is top-level; --profile=NAME is not applicable"
        )


def _ensure_local_yaml(yaml_path: Path) -> None:
    """Ensure ``local.yaml`` exists before mutating it."""
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    if not yaml_path.exists():
        ensure_local_config_stub()


def _run_tracked_git_check(yaml_path: Path) -> None:
    """Refuse tracked-side mutation when the config repo is dirty.

    Builds an on-the-fly :class:`PathSource` pointing at the tracked
    config directory and delegates to :func:`run_git_check_or_raise`
    (cli/_git_check.py:500). Mirrors the install / sync gate: dirty
    working tree → user must commit or stash before mutating.
    """
    from setforge.source import PathSource, SourceKind

    fake_source = PathSource(kind=SourceKind.PATH, path=yaml_path.parent)
    run_git_check_or_raise(source=fake_source, no_git_check=False)


@config_app.command("add")
def config_add(
    path: str = typer.Argument(
        ...,
        help="Dotted-path (e.g. plugins.add, binaries.code).",
        autocompletion=lambda ctx, incomplete: _complete_path_dispatch(ctx, incomplete),
    ),
    value: str = typer.Argument(
        ...,
        help="Value to append (lists) or set (scalars).",
        autocompletion=lambda ctx, incomplete: _complete_value(ctx, incomplete),
    ),
    local: bool = typer.Option(False, "--local"),
    tracked: bool = typer.Option(False, "--tracked"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    yes: bool = typer.Option(False, "--yes", help="Skip the arrow-key confirm."),
    # Interactive marketplaces.add sibling flags (non-TTY fallback per A29).
    source: str | None = typer.Option(
        None, "--source", help="Source kind for marketplaces.add (github | path)."
    ),
    repo: str | None = typer.Option(
        None, "--repo", help="Repo slug for github-kind marketplaces.add."
    ),
) -> None:
    """Append-to-list OR set-scalar at the dotted path."""
    scope = _resolve_scope(local=local, tracked=tracked)
    _check_profile_arg(path, profile)
    if path == "marketplaces.add":
        _add_marketplace(scope, value, source=source, repo=repo, yes=yes)
        return
    node = _resolve_path(scope, path)
    if node is None:
        raise SetforgeError(f"unknown path for --{scope.value}: {path!r}")
    yaml_path = _scope_yaml_path(scope)
    if scope is ConfigScope.LOCAL:
        _ensure_local_yaml(yaml_path)
    elif scope is ConfigScope.TRACKED:
        _run_tracked_git_check(yaml_path)
    _mutate_and_write(
        scope=scope,
        yaml_path=yaml_path,
        dotted=path,
        op_value=value,
        op="add",
        is_list=node.is_list,
        yes=yes,
    )


@config_app.command("remove")
def config_remove(
    path: str = typer.Argument(
        ...,
        autocompletion=lambda ctx, incomplete: _complete_path_dispatch(ctx, incomplete),
    ),
    value: str | None = typer.Argument(
        None,
        help="List-remove value; omit for scalar-unset.",
        autocompletion=lambda ctx, incomplete: _complete_value(ctx, incomplete),
    ),
    local: bool = typer.Option(False, "--local"),
    tracked: bool = typer.Option(False, "--tracked"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Pop-from-list OR unset-scalar at the dotted path."""
    scope = _resolve_scope(local=local, tracked=tracked)
    _check_profile_arg(path, profile)
    node = _resolve_path(scope, path)
    if node is None:
        raise SetforgeError(f"unknown path for --{scope.value}: {path!r}")
    yaml_path = _scope_yaml_path(scope)
    if scope is ConfigScope.TRACKED:
        _run_tracked_git_check(yaml_path)
    _mutate_and_write(
        scope=scope,
        yaml_path=yaml_path,
        dotted=path,
        op_value=value,
        op="remove",
        is_list=node.is_list,
        yes=yes,
    )


def _mutate_and_write(
    *,
    scope: ConfigScope,
    yaml_path: Path,
    dotted: str,
    op_value: str | None,
    op: str,
    is_list: bool,
    yes: bool,
) -> None:
    """Apply mutation, validate, diff-preview, confirm, atomic-write."""
    before_text = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
    doc = _load_doc(yaml_path)
    if op == "add":
        if op_value is None:
            raise SetforgeError("add requires <value>")
        _apply_add(doc, dotted, op_value, is_list=is_list)
    else:
        _apply_remove(doc, dotted, op_value, is_list=is_list)
    _validate_candidate(scope, doc)
    after_text = _dump_to_str(doc)
    diff_text = _render_diff(before_text, after_text, yaml_path)
    console = Console()
    if not _prompt_confirm(
        yaml_path=yaml_path, diff_text=diff_text, console=console, yes=yes
    ):
        return
    atomic_write_yaml(yaml_path, doc)


# ---------------------------------------------------------------------------
# Interactive marketplaces.add (A29)
# ---------------------------------------------------------------------------


def _add_marketplace(
    scope: ConfigScope,
    name: str,
    *,
    source: str | None,
    repo: str | None,
    yes: bool,
) -> None:
    """Handle ``setforge config add --local marketplaces.add <name>``.

    Non-TTY: requires ``--source`` + (``--repo`` for github).
    Interactive: prompts via lazy-imported prompt_toolkit dialogs.
    Validates the candidate MarketplaceSource, then diff-previews +
    atomic-writes back through ``_mutate_and_write``.
    """
    if scope is not ConfigScope.LOCAL:
        raise SetforgeError("marketplaces.add only supported on --local for now")
    yaml_path = _scope_yaml_path(scope)
    _ensure_local_yaml(yaml_path)
    resolved_source, resolved_repo, resolved_path = _resolve_marketplace_inputs(
        source=source, repo=repo, yes=yes
    )
    # Validate the candidate MarketplaceSource shape BEFORE writing.
    candidate = MarketplaceSource.model_validate(
        {
            "source": resolved_source,
            "repo": resolved_repo,
            "path": resolved_path,
        }
    )
    doc = _load_doc(yaml_path)
    before_text = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
    if "marketplaces" not in doc:
        doc["marketplaces"] = CommentedMap()
    if name in doc["marketplaces"]:
        raise SetforgeError(f"marketplaces.{name} already exists")
    entry = CommentedMap()
    entry["source"] = candidate.source.value
    if candidate.repo is not None:
        entry["repo"] = candidate.repo
    if candidate.path is not None:
        entry["path"] = str(candidate.path)
    doc["marketplaces"][name] = entry
    after_text = _dump_to_str(doc)
    diff_text = _render_diff(before_text, after_text, yaml_path)
    console = Console()
    if not _prompt_confirm(
        yaml_path=yaml_path, diff_text=diff_text, console=console, yes=yes
    ):
        return
    atomic_write_yaml(yaml_path, doc)


def _resolve_marketplace_inputs(
    *, source: str | None, repo: str | None, yes: bool
) -> tuple[str, str | None, str | None]:
    """Resolve marketplaces.add inputs via flags or interactive dialogs."""
    if source is not None:
        if source not in (
            MarketplaceSourceKind.GITHUB.value,
            MarketplaceSourceKind.PATH.value,
        ):
            raise SetforgeError(
                f"--source must be one of github | path; got {source!r}"
            )
        if source == MarketplaceSourceKind.GITHUB.value and repo is None:
            raise SetforgeError("--source=github requires --repo=owner/name")
        if source == MarketplaceSourceKind.PATH.value:
            return source, None, repo  # `repo` reused as path string
        return source, repo, None
    if not sys.stdin.isatty() or yes:
        raise ConfirmRequiresInteractive(
            "marketplaces.add requires --source + (--repo or path) when stdin "
            "is not a TTY"
        )
    from setforge.cli import config as _self

    chosen = _self.radiolist_dialog(
        title="setforge config add marketplaces.add",
        text="Pick the source kind for this marketplace:",
        values=[
            (MarketplaceSourceKind.GITHUB.value, "github (owner/name)"),
            (MarketplaceSourceKind.PATH.value, "path (local clone)"),
        ],
        default=MarketplaceSourceKind.GITHUB.value,
    ).run()
    if chosen is None:
        raise SetforgeError("marketplaces.add aborted (no source picked)")
    if chosen == MarketplaceSourceKind.GITHUB.value:
        repo_str = _self.input_dialog(title="repo slug", text="owner/name:").run()
        if not repo_str:
            raise SetforgeError("marketplaces.add aborted (no repo entered)")
        return chosen, repo_str, None
    path_str = _self.input_dialog(title="path", text="absolute path:").run()
    if not path_str:
        raise SetforgeError("marketplaces.add aborted (no path entered)")
    return chosen, None, path_str


# ---------------------------------------------------------------------------
# Shell completion callbacks
# ---------------------------------------------------------------------------


def _scope_from_ctx(ctx: typer.Context) -> ConfigScope | None:
    """Read ``--local`` / ``--tracked`` from a typer Context safely.

    Falls back to ``None`` when neither was set yet — completion fires
    EARLY (before all params are resolved), so callbacks must tolerate
    a half-built context. Returns ``None`` → completion uses local
    scope as default for usability.
    """
    params = getattr(ctx, "params", {}) or {}
    if params.get("local"):
        return ConfigScope.LOCAL
    if params.get("tracked"):
        return ConfigScope.TRACKED
    return None


def _complete_path_dispatch(ctx: typer.Context, incomplete: str) -> list[str]:
    """Dispatch path completion based on the resolved scope."""
    scope = _scope_from_ctx(ctx) or ConfigScope.LOCAL
    try:
        if scope is ConfigScope.LOCAL:
            return _complete_path_local(ctx, incomplete)
        return _complete_path_tracked(ctx, incomplete)
    except Exception:
        return _static_template_paths(scope)


def _complete_path_local(ctx: typer.Context, incomplete: str) -> list[str]:
    """Yield dotted-paths under the local schema matching ``incomplete``."""
    return [p for p in _enumerate_paths(ConfigScope.LOCAL) if p.startswith(incomplete)]


def _complete_path_tracked(ctx: typer.Context, incomplete: str) -> list[str]:
    """Yield dotted-paths under the tracked Config schema matching ``incomplete``."""
    return [
        p for p in _enumerate_paths(ConfigScope.TRACKED) if p.startswith(incomplete)
    ]


def _static_template_paths(scope: ConfigScope) -> list[str]:
    """Static fallback list of top-level keys when dynamic completion fails."""
    if scope is ConfigScope.LOCAL:
        return ["source", "binaries", "claude"]
    return [
        "version",
        "schema_version",
        "tracked_files",
        "marketplaces",
        "claude_plugins",
        "profiles",
    ]


def _complete_value(ctx: typer.Context, incomplete: str) -> list[str]:
    """Dispatch value completion on resolved path + verb.

    - list-add: candidates from universe MINUS current list.
    - list-remove: current list members.
    - scalar-enum: enum values.
    - scalar-free: empty.
    """
    try:
        return _complete_value_impl(ctx, incomplete)
    except Exception:
        return []


def _complete_value_impl(ctx: typer.Context, incomplete: str) -> list[str]:
    """Body of ``_complete_value`` (separated for top-level fallback try)."""
    params = getattr(ctx, "params", {}) or {}
    path = params.get("path")
    if not path:
        return []
    scope = _scope_from_ctx(ctx) or ConfigScope.LOCAL
    node = _resolve_path(scope, path)
    if node is None:
        return []
    info = getattr(ctx, "info_name", None) or ""
    verb_remove = info == "remove"
    if node.is_list:
        yaml_path = _scope_yaml_path(scope)
        doc = _load_doc(yaml_path)
        try:
            current = _slice_doc(doc, path)
        except SetforgeError:
            current = []
        current_list = (
            list(current) if isinstance(current, (list, CommentedSeq)) else []
        )
        if verb_remove:
            return [str(x) for x in current_list if str(x).startswith(incomplete)]
        # add-to-list: empty universe today (no marketplace registry pull
        # at completion time — too slow). Surface "current minus self"
        # for parity; over time we may seed a project-local cache.
        return [x for x in current_list if str(x).startswith(incomplete)]
    if node.enum_values:
        return [v for v in node.enum_values if v.startswith(incomplete)]
    return []
