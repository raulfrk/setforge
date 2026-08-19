"""``setforge snapshot {create,list,restore}`` — directory-copy snapshots.

Thin typer subgroup over :mod:`setforge.snapshots`: each command parses
flags, builds a :class:`ProfileContext` when needed, calls the domain
helper, and renders the result. Restore presents an arrow-key three-way
choice (abort / restore / restore-with-pre-snapshot); ``--yes`` /
``--non-interactive`` short-circuits to plain "restore". Restore plans
are profile-scoped and journaled before the first live write.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path
from typing import Any, assert_never

import typer
from rich.console import Console
from rich.table import Table

from setforge import operations
from setforge import snapshots as snap_mod
from setforge._redact import redact_argv
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._help_examples import (
    SNAPSHOT_CREATE_EXAMPLES,
    SNAPSHOT_LIST_EXAMPLES,
    SNAPSHOT_RESTORE_EXAMPLES,
)
from setforge.cli._helpers import ProfileContext
from setforge.config import load_config, resolve_effective_profile
from setforge.errors import SetforgeError
from setforge.locking import mutation_locks
from setforge.transitions import now_utc as _now_utc


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "button_bar":
        from setforge.ui.widgets import button_bar

        return button_bar
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "RestoreChoice",
    "snapshot_app",
    "snapshot_create",
    "snapshot_list",
    "snapshot_restore",
]


snapshot_app: typer.Typer = typer.Typer(
    help=(
        "Capture / inspect / restore directory-copy snapshots of the "
        "profile-resolved tracked_files.dst set plus local.yaml."
    ),
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(snapshot_app, name="snapshot")


class RestoreChoice(StrEnum):
    """Outcome of the restore-confirm arrow-key prompt."""

    ABORT = "abort"
    RESTORE = "restore"
    RESTORE_WITH_PRE_SNAPSHOT = "restore-with-pre-snapshot"


def _build_profile_ctx(profile: str, config: Path) -> ProfileContext:
    """Resolve ``--profile`` / ``--config`` into a :class:`ProfileContext`."""
    resolved_config = _resolve_config_arg(config)
    cfg = load_config(resolved_config)
    repo_root = resolved_config.resolve().parent
    resolved = resolve_effective_profile(cfg, profile, repo_root).resolved
    return ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )


def _emit_create_summary(meta: snap_mod.SnapshotMeta, *, console: Console) -> None:
    """Print the post-create banner used by ``snapshot create``.

    Distinguishes the tracked_files.dst paths from the host-local
    ``local.yaml`` so the count line reads as the user expects
    ("capturing N tracked_files.dst paths"). ``LOCAL_CONFIG_PATH`` is
    read off ``snap_mod`` so tests' monkeypatch of the constant flows
    through to the banner.
    """
    size_bytes = snap_mod.directory_size_bytes(meta.snapshot_id)
    root = snap_mod.snapshots_root() / meta.snapshot_id
    local_yaml = snap_mod.LOCAL_CONFIG_PATH
    local_yaml_captured = local_yaml in meta.files
    tracked_count = len(meta.files) - (1 if local_yaml_captured else 0)
    console.print(f"=== creating snapshot {meta.label!r} ===")
    console.print(
        f"  capturing {tracked_count} tracked_files.dst paths from "
        f"profile {meta.profile}"
    )
    if local_yaml_captured:
        console.print(f"  capturing {local_yaml}")
    console.print(f"  storing in: {root}/")
    console.print(f"  total size: {snap_mod.format_size(size_bytes)}")
    console.print("=== snapshot complete ===")
    console.print(f"  to restore: setforge snapshot restore {meta.label}")


@snapshot_app.command("create", epilog=SNAPSHOT_CREATE_EXAMPLES)
def snapshot_create(
    label: str = typer.Argument(
        ...,
        help="Short identifier for this snapshot (e.g. 'before-experiment').",
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    keep: int = typer.Option(
        snap_mod.DEFAULT_KEEP,
        "--keep",
        help="Retain at most this many snapshots after create "
        "(0 = remove all; negative is rejected).",
        show_default=True,
    ),
) -> None:
    """Capture the profile's live state into a new snapshot."""
    resolved_config = _resolve_config_arg(config)
    with mutation_locks(config_dir=resolved_config.resolve().parent, profile=profile):
        operations.refuse_active(profile)
        ctx = _build_profile_ctx(profile, resolved_config)
        meta = snap_mod.create_snapshot(
            ctx.cfg, ctx.resolved, ctx.repo_root, ctx.profile, label, keep=keep
        )
    _emit_create_summary(meta, console=Console())


@snapshot_app.command("list", epilog=SNAPSHOT_LIST_EXAMPLES)
def snapshot_list() -> None:
    """List every snapshot under ``~/.local/share/setforge/snapshots/``.

    Newest first, columns: ``id`` / ``label`` / ``age`` / ``size``. Empty
    snapshot-root prints a single-line "no snapshots yet" hint.
    """
    snaps = snap_mod.list_snapshots()
    console = Console()
    if not snaps:
        console.print(
            "no snapshots yet — run 'setforge snapshot create <label> --profile=<name>'"
        )
        return
    now = _now_utc()
    table = Table(show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("label")
    table.add_column("age")
    table.add_column("size", justify="right")
    for snap in snaps:
        size_bytes = snap_mod.directory_size_bytes(snap.snapshot_id)
        table.add_row(
            snap.snapshot_id,
            snap.label,
            snap_mod.format_age(now, snap.created_at),
            snap_mod.format_size(size_bytes),
        )
    console.print(table)


def _stdin_is_tty() -> bool:
    """Module-level indirection so tests can monkeypatch the TTY check."""
    return sys.stdin.isatty()


def _format_overwrite_groups(files: tuple[Path, ...]) -> str:
    """Render the captured live paths as ``<dir>/* + <dir>/* + ...``.

    Groups files by parent directory; each parent appears once as
    ``<parent>/*`` if it holds more than one file, or as the bare path
    if it's the only entry under that parent. Order matches first-seen
    encounter in ``files`` so the banner stays stable for a given
    snapshot.
    """
    seen: dict[Path, list[Path]] = {}
    for path in files:
        seen.setdefault(path.parent, []).append(path)
    parts: list[str] = []
    for parent, members in seen.items():
        if len(members) == 1:
            parts.append(str(members[0]))
        else:
            parts.append(f"{parent}/*")
    return " + ".join(parts)


def _prompt_restore_choice(
    target: snap_mod.SnapshotMeta, *, console: Console
) -> RestoreChoice:
    """Render the arrow-key three-way restore confirm.

    Returns the user's choice; ``None`` from the dialog (Esc) is treated
    as :attr:`RestoreChoice.ABORT`.

    Mutate-gate: when stdin is not a TTY, raise
    :class:`SetforgeError` rather than silently proceed.
    """
    if not _stdin_is_tty():
        raise SetforgeError(
            "snapshot restore: requires --yes (or --non-interactive) when "
            "stdin is not a TTY"
        )
    console.print(f"[bold]=== restoring snapshot {target.label!r} ===[/bold]")
    console.print(
        f"  WILL overwrite live files at: {_format_overwrite_groups(target.files)}"
    )
    console.print(
        "  [yellow]additive overlay[/yellow]: live-only files NOT in this "
        "snapshot will be left alone"
    )
    from setforge.cli import snapshot as _self  # local alias for monkeypatch
    from setforge.ui.widgets import CANCEL, Button

    choice = _self.button_bar(
        [
            Button("no, abort", RestoreChoice.ABORT),
            Button("yes, restore", RestoreChoice.RESTORE),
            Button(
                "yes + write a new snapshot of current state first",
                RestoreChoice.RESTORE_WITH_PRE_SNAPSHOT,
            ),
        ],
        title="setforge snapshot restore",
        body="Proceed?",
        initial=0,
    )
    if choice is CANCEL:
        return RestoreChoice.ABORT
    return choice


def _emit_restore_summary(meta: snap_mod.SnapshotMeta, *, console: Console) -> None:
    """Print the post-restore banner used by ``snapshot restore``."""
    console.print(f"  restored from {meta.label} ({len(meta.files)} files overlaid)")
    console.print("=== restore complete ===")


@snapshot_app.command("restore", epilog=SNAPSHOT_RESTORE_EXAMPLES)
def snapshot_restore(
    snapshot: str = typer.Argument(
        ...,
        help="Snapshot id (e.g. '20260518T210000Z-before-experiment') "
        "or label (e.g. 'before-experiment').",
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the arrow-key confirm; restore without writing a "
        "pre-restore snapshot.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Synonym of --yes — for CI / cron contexts.",
    ),
) -> None:
    """Overlay a snapshot's captured files onto live (ADDITIVE per Q6).

    Live-only files added since the snapshot are left untouched; only
    files that exist in the snapshot are overlaid. The snapshot must
    belong to the requested effective profile, and a partial failure is
    recovered from the durable pre-write journal. Interactive runs
    present a three-option arrow-key confirm (abort / restore /
    restore+pre-restore-snapshot); non-interactive runs (``--yes`` /
    ``--non-interactive``) bypass the wizard and do a plain restore
    (no pre-restore snapshot — opt into that via the interactive
    choice).
    """
    target = snap_mod.resolve_snapshot(snapshot, profile=profile)
    skip_prompt = yes or non_interactive
    console = Console()
    if skip_prompt:
        choice = RestoreChoice.RESTORE
    else:
        choice = _prompt_restore_choice(target, console=console)
    if choice is RestoreChoice.ABORT:
        console.print("[red]aborted[/red] — no live mutations applied")
        raise typer.Exit(code=1)
    resolved_config = _resolve_config_arg(config)
    config_dirs = {
        resolved_config.resolve().parent,
        *(
            (snap_mod.LOCAL_CONFIG_PATH.parent,)
            if snap_mod.LOCAL_CONFIG_PATH in target.files
            else ()
        ),
    }
    with (
        mutation_locks(
            config_dirs=tuple(config_dirs),
            profile=profile,
        ),
        operations.recover_on_error(profile, "snapshot restore"),
    ):
        ctx = _build_profile_ctx(profile, resolved_config)
        plan = snap_mod._plan_restore_snapshot(
            snapshot,
            cfg=ctx.cfg,
            resolved=ctx.resolved,
            repo_root=ctx.repo_root,
            profile=ctx.profile,
        )
        if plan.target != target:
            raise SetforgeError("snapshot selection changed after confirmation; retry")
        snap_mod._validate_restore_plan(plan)
        match choice:
            case RestoreChoice.RESTORE:
                pass
            case RestoreChoice.RESTORE_WITH_PRE_SNAPSHOT:
                snap_mod.create_snapshot(
                    ctx.cfg,
                    ctx.resolved,
                    ctx.repo_root,
                    ctx.profile,
                    f"pre-restore-{target.snapshot_id}",
                )
            case _:  # pragma: no cover — exhaustive over StrEnum
                assert_never(choice)
        journal = operations.prepare(
            command="snapshot restore",
            profile=profile,
            config_dir=resolved_config.resolve().parent,
            config_dirs=tuple(config_dirs),
            resources_lock=False,
            command_line=tuple(redact_argv(sys.argv[1:])),
            paths=tuple(file.path for file in plan.files),
            path_guards=plan.destination_ancestors,
        )
        journal = operations.begin_checkpoint(
            journal,
            name="restore-files",
            kind=operations.CheckpointKind.REVERSIBLE,
            recovery="restore every live path captured before snapshot restore",
            restore_state=False,
            restore_transitions=False,
            adapters=(),
        )
        try:
            snap_mod._validate_restore_plan(plan)
        except BaseException as primary:
            try:
                operations.complete(journal)
            except BaseException as cleanup_error:
                primary.add_note(
                    "snapshot restore journal cleanup failed after a no-effect "
                    f"preflight refusal: {cleanup_error}"
                )
            raise
        snap_mod._apply_restore_plan(plan, validate=False)
        journal = operations.finish_checkpoint(journal)
        operations.complete(journal)
        _emit_restore_summary(plan.target, console=console)
