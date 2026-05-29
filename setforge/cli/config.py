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
import io
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from ruamel.yaml.comments import (
    CommentedMap,
    CommentedSeq,
)

from setforge.binaries import LOCAL_CONFIG_PATH, ensure_local_config_stub
from setforge.cli import app
from setforge.cli._config_helpers import (
    FieldNode as _FieldNode,
)
from setforge.cli._config_helpers import (
    apply_add as _apply_add,
)
from setforge.cli._config_helpers import (
    apply_remove as _apply_remove,
)
from setforge.cli._config_helpers import (
    enumerate_paths as _enumerate_paths_inner,
)
from setforge.cli._config_helpers import (
    load_doc as _load_doc,
)
from setforge.cli._config_helpers import (
    resolve_path as _resolve_path_inner,
)
from setforge.cli._config_helpers import (
    to_plain as _to_plain,
)
from setforge.cli._config_helpers import (
    walk_model as _walk_model,
)
from setforge.cli._git_check import run_git_check_or_raise
from setforge.cli._output import OutputContext
from setforge.config import Config, MarketplaceSource, MarketplaceSourceKind
from setforge.errors import ConfirmRequiresInteractive, SetforgeError
from setforge.local_config import LocalConfig
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


def _module_self() -> Any:  # noqa: ANN401
    """Return this module so callers can resolve monkeypatched PEP 562 attrs.

    The :func:`_prompt_confirm` / :func:`_prompt_marketplace_kind`
    helpers reach for ``radiolist_dialog`` / ``input_dialog`` via this
    module so tests that monkeypatch
    ``setforge.cli.config.radiolist_dialog`` intercept the live
    reference. Defining the indirection once at module scope keeps
    the per-call sites tight.
    """
    return sys.modules[__name__]


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
    rich_markup_mode=None,
)
app.add_typer(config_app, name="config")


# ---------------------------------------------------------------------------
# Scope resolution + tracked-config path discovery
# ---------------------------------------------------------------------------


_MISSING_SCOPE_TWO_WAY: Final[str] = "exactly one of --local / --tracked is required"
"""Error message when neither ``--local`` nor ``--tracked`` is set on add / remove."""

_MISSING_SCOPE_THREE_WAY: Final[str] = (
    "exactly one of --local / --tracked / --effective is required"
)
"""Error when no scope flag is set on ``show`` (the three-way form)."""


def _resolve_scope(
    *,
    local: bool,
    tracked: bool,
    effective: bool = False,
    allow_effective: bool = False,
) -> ConfigScope:
    """Enforce the ``--local`` / ``--tracked`` / ``--effective`` mutex.

    Exactly-one-required (no implicit default) raises
    :class:`typer.BadParameter` so the user sees a typer-formatted
    error rather than a stack trace. ``--effective`` is only valid on
    ``show``; callers pass ``allow_effective=True`` there and leave
    ``allow_effective=False`` for ``add`` / ``remove`` so the error
    message omits the inapplicable third flag.
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
            _MISSING_SCOPE_THREE_WAY if allow_effective else _MISSING_SCOPE_TWO_WAY
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
    """Return the tracked ``setforge.yaml`` resolved via the source layer.

    :func:`validate_source_dir` already returns the full path to the
    ``setforge.yaml`` file inside the resolved source directory, not the
    directory itself. The earlier ``source_dir / "setforge.yaml"`` form
    double-suffixed the filename (yielding e.g.
    ``/path/setforge.yaml/setforge.yaml``), which then tripped
    :func:`_run_tracked_git_check`'s ``yaml_path.parent`` to point at a
    file rather than a directory.
    """
    resolved_source = get_resolved_source()
    return validate_source_dir(resolved_source)


def _scope_yaml_path(scope: ConfigScope) -> Path:
    """Return the on-disk YAML file for ``scope`` (local | tracked)."""
    if scope is ConfigScope.LOCAL:
        return LOCAL_CONFIG_PATH
    if scope is ConfigScope.TRACKED:
        return _tracked_yaml_path()
    raise SetforgeError(f"_scope_yaml_path: unexpected scope {scope!r}")


# ---------------------------------------------------------------------------
# Schema walk (cached) — wraps _config_helpers with per-scope dispatch
# ---------------------------------------------------------------------------


@functools.cache
def _schema_local() -> dict[str, _FieldNode]:
    """Cached walk of the local-scope model schema."""
    # Use LocalConfig (the validate-only union of source/binaries/claude).
    return _walk_model(LocalConfig)


@functools.cache
def _schema_tracked() -> dict[str, _FieldNode]:
    """Cached walk of the tracked-scope ``Config`` model schema."""
    return _walk_model(Config)


def _resolve_path(scope: ConfigScope, dotted: str) -> _FieldNode | None:
    """Resolve a dotted path against the cached schema tree for ``scope``."""
    schema = _schema_local() if scope is ConfigScope.LOCAL else _schema_tracked()
    return _resolve_path_inner(schema, dotted)


def _enumerate_paths(scope: ConfigScope) -> list[str]:
    """Yield every concrete dotted path under ``scope``'s schema."""
    schema = _schema_local() if scope is ConfigScope.LOCAL else _schema_tracked()
    return _enumerate_paths_inner(schema)


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
            LocalConfig.model_validate(plain)
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
    else:
        raise SetforgeError(f"_validate_candidate: unexpected scope {scope!r}")


# ---------------------------------------------------------------------------
# Diff preview + confirm panel
# ---------------------------------------------------------------------------


def _dump_to_str(doc: Any) -> str:  # noqa: ANN401 — accepts any YAML-dumpable tree
    """Serialize ``doc`` back to a string using the rt YAML factory.

    Accepts any YAML-dumpable tree (``CommentedMap`` for whole-file
    dumps, plus ``CommentedSeq`` / scalar / dict / list for the sliced
    sub-trees produced by :func:`_slice_doc` in ``show``).
    """
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
    _self = _module_self()
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
    ctx: typer.Context,
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
    scope = _resolve_scope(
        local=local, tracked=tracked, effective=effective, allow_effective=True
    )
    if scope is ConfigScope.EFFECTIVE:
        if profile is None:
            raise typer.BadParameter("--effective requires --profile=NAME")
        _show_effective(profile, ctx_obj=ctx.obj)
        return
    yaml_path = _scope_yaml_path(scope)
    doc = _load_doc(yaml_path)
    sliced = _slice_doc(doc, path)
    typer.echo(_dump_to_str(sliced).rstrip())


def _show_effective(profile: str, *, ctx_obj: OutputContext | None) -> None:
    """Print the merged profile chain via the existing ``profile show`` body.

    Delegates to :func:`_run_profile_show` — the typer-context-free
    inner helper extracted from the ``profile show`` subcommand —
    rather than calling the typer-decorated ``profile_show`` directly,
    which requires a :class:`typer.Context` first positional argument.

    ``ctx_obj`` is threaded from ``config_show``'s typer context so
    ``render()`` sees a real :class:`OutputContext` outside test runs;
    passing ``None`` here would trip the production guard in
    :func:`setforge.cli._output.render`.
    """
    from setforge.cli.profile import _run_profile_show

    cfg_path = _tracked_yaml_path()
    _run_profile_show(name=profile, config=cfg_path, ctx_obj=ctx_obj)


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
    _run_add(
        path=path,
        value=value,
        local=local,
        tracked=tracked,
        profile=profile,
        yes=yes,
        source=source,
        repo=repo,
    )


def _run_add(
    *,
    path: str,
    value: str,
    local: bool,
    tracked: bool,
    profile: str | None,
    yes: bool,
    source: str | None,
    repo: str | None,
) -> None:
    """Internal body of ``config_add`` — kept separate from the typer shim.

    The typer.Argument / typer.Option declarations on :func:`config_add`
    inflate that function's surface area; the actual orchestration
    lives here so the per-step logic (scope resolution, marketplace
    special case, schema lookup, git gate, mutate-and-write) stays
    readable.
    """
    scope = _resolve_scope(local=local, tracked=tracked)
    _check_profile_arg(path, profile)
    if path == "marketplaces.add":
        _add_marketplace(scope, value, source=source, repo=repo, yes=yes)
        return
    node = _resolve_path(scope, path)
    if node is None:
        raise SetforgeError(f"unknown path for --{scope.value}: {path!r}")
    yaml_path = _prepare_scope_yaml(scope)
    _mutate_and_write(
        scope=scope,
        yaml_path=yaml_path,
        dotted=path,
        op_value=value,
        op="add",
        is_list=node.is_list,
        yes=yes,
    )


def _prepare_scope_yaml(scope: ConfigScope) -> Path:
    """Return the yaml path for ``scope``, ensuring the per-scope precondition.

    LOCAL: stub the file if missing so the mutate-then-write pipeline
    has a doc to load. TRACKED: refuse on a dirty config repo via the
    install / sync git-clean gate. Returns the resolved path either way.
    """
    yaml_path = _scope_yaml_path(scope)
    if scope is ConfigScope.LOCAL:
        _ensure_local_yaml(yaml_path)
    elif scope is ConfigScope.TRACKED:
        _run_tracked_git_check(yaml_path)
    return yaml_path


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
    if scope is ConfigScope.LOCAL and not yaml_path.exists():
        typer.echo("nothing to remove")
        raise typer.Exit(0)
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
    op: Literal["add", "remove"],
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
    _preview_and_write(yaml_path=yaml_path, doc=doc, before_text=before_text, yes=yes)


def _preview_and_write(
    *, yaml_path: Path, doc: CommentedMap, before_text: str, yes: bool
) -> None:
    """Render diff → prompt confirm → atomic-write the candidate doc.

    Shared tail for both the generic ``_mutate_and_write`` path and the
    marketplaces.add-specific ``_add_marketplace`` flow. Returns
    silently when the user declines (``_prompt_confirm`` returns
    ``False``); the file is untouched.
    """
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
    doc["marketplaces"][name] = _build_marketplace_entry(candidate)
    _preview_and_write(yaml_path=yaml_path, doc=doc, before_text=before_text, yes=yes)


def _build_marketplace_entry(candidate: MarketplaceSource) -> CommentedMap:
    """Render a validated :class:`MarketplaceSource` as a YAML mapping entry.

    Emits the ``source`` / ``repo`` / ``path`` keys in stable order with
    ``None`` slots omitted, so the round-trip serializer doesn't leak
    spurious null lines into the diff preview.
    """
    entry = CommentedMap()
    entry["source"] = candidate.source.value
    if candidate.repo is not None:
        entry["repo"] = candidate.repo
    if candidate.path is not None:
        entry["path"] = str(candidate.path)
    return entry


def _resolve_marketplace_inputs(
    *, source: str | None, repo: str | None, yes: bool
) -> tuple[str, str | None, str | None]:
    """Resolve marketplaces.add inputs via flags or interactive dialogs.

    Two-phase dispatch: ``--source`` flag set → :func:`_resolve_from_flags`
    (no interactive prompt). Otherwise gate on a real TTY and dispatch
    to :func:`_prompt_marketplace_kind` for the interactive flow.
    Returns the ``(kind, repo, path)`` triple consumed by
    :func:`_add_marketplace`.
    """
    if source is not None:
        return _resolve_from_flags(source=source, repo=repo)
    if not sys.stdin.isatty() or yes:
        raise ConfirmRequiresInteractive(
            "marketplaces.add requires --source + (--repo or path) when stdin "
            "is not a TTY"
        )
    return _prompt_marketplace_kind()


def _resolve_from_flags(
    *, source: str, repo: str | None
) -> tuple[str, str | None, str | None]:
    """Resolve marketplaces.add inputs from flag values (non-interactive path).

    ``--repo`` aliases two distinct slots: for ``--source=github`` it
    carries the ``owner/name`` slug; for ``--source=path`` it carries
    the absolute filesystem path. Returns ``(kind, repo, None)`` for
    github or ``(kind, None, path)`` for path, matching the
    discriminated MarketplaceSource union.
    """
    if source not in (
        MarketplaceSourceKind.GITHUB.value,
        MarketplaceSourceKind.PATH.value,
    ):
        raise SetforgeError(f"--source must be one of github | path; got {source!r}")
    if source == MarketplaceSourceKind.GITHUB.value:
        if repo is None:
            raise SetforgeError("--source=github requires --repo=owner/name")
        return source, repo, None
    # --source=path — ``repo`` carries the absolute path string.
    return source, None, repo


def _prompt_marketplace_kind() -> tuple[str, str | None, str | None]:
    """Drive the interactive marketplaces.add flow via prompt_toolkit dialogs.

    Lazy-imports the module-level radiolist/input dialog symbols so
    monkeypatch indirection from the unit tests still resolves through
    the live module object.
    """
    _self = _module_self()
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
    """Dispatch path completion based on the resolved scope.

    Shell-completion contract: NEVER raise on user input — falling back
    to the static-template top-level list keeps tab from breaking the
    shell when config parsing / schema-walk surfaces an unexpected
    failure (per anti-smell #17). Narrow the catch to the exception
    families the schema walk / Pydantic introspection can plausibly
    raise so a SystemExit / KeyboardInterrupt still propagates.
    """
    scope = _scope_from_ctx(ctx) or ConfigScope.LOCAL
    try:
        if scope is ConfigScope.LOCAL:
            return _complete_path_local(ctx, incomplete)
        return _complete_path_tracked(ctx, incomplete)
    except (SetforgeError, KeyError, AttributeError, ValueError, OSError):
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
    except (SetforgeError, KeyError, AttributeError, ValueError, OSError):
        # Shell-completion contract: never raise on user input. Narrow
        # the catch to plausible failure modes (missing config file,
        # malformed YAML, half-built typer.Context) so SystemExit /
        # KeyboardInterrupt still propagates.
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
