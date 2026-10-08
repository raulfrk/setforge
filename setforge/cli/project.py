"""CLI for reversible project-profile injection, sync, and removal."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import (
    PROJECT_INJECT_EXAMPLES,
    PROJECT_LIST_EXAMPLES,
    PROJECT_REMOVE_EXAMPLES,
    PROJECT_SYNC_EXAMPLES,
    PROJECT_VISIBILITY_EXAMPLES,
)
from setforge.config import ProjectVisibility, load_config, resolve_project_profile
from setforge.errors import ConfirmRequiresInteractive, SetforgeError
from setforge.project_injection import (
    ProjectInjectionPlan,
    ProjectRemovePlan,
    ProjectStaleRemovalPlan,
    apply_injection,
    apply_removal,
    apply_stale_removal,
    convert_older_records,
    plan_injection,
    plan_removal,
    plan_stale_removal,
    resolve_injection_plan,
)
from setforge.project_overlay import process_filter
from setforge.project_record import ProjectFileAction
from setforge.project_sync import (
    AutoResolution,
    ProjectSyncPlan,
    apply_sync,
    missing_locally,
    plan_sync,
    resolve_sync_plan,
)
from setforge.project_visibility import (
    apply_project_visibility,
    list_projects,
    plan_project_visibility,
)
from setforge.reconcile.merge_model import Conflict

project_app = typer.Typer(
    help="Inject, synchronize, and remove reusable files in a project worktree.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(project_app, name="project")


@project_app.command("filter-process", hidden=True)
def project_filter_process() -> None:
    """Serve the private Git filter protocol for tracked project overlays."""
    process_filter()


def _confirm(command: str, *, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            f"setforge project {command} requires --yes when stdin is not a TTY"
        )
    return typer.confirm(f"Proceed with project {command}?", default=False)


def _convert_older_records(path: Path, *, dry_run: bool) -> None:
    """Bring this directory's older-format records up to date before planning.

    A preview changes nothing, so it leaves an older record to be refused.
    """
    if dry_run:
        return
    for profile in convert_older_records(path):
        typer.echo(
            f"converted the injection record of project profile {profile!r} "
            "to the current format"
        )


def _render_injection(plan: ProjectInjectionPlan) -> None:
    typer.echo(f"project profile: {plan.profile}")
    typer.echo(f"target: {plan.target}")
    if plan.git_dir is None:
        typer.echo("Git visibility: not applicable (target is not a Git worktree)")
    else:
        typer.echo(f"Git visibility: {plan.visibility.value}")
    if plan.visibility_plan is not None and plan.visibility_plan.changed:
        action = (
            "add private exclude claims"
            if plan.visibility_plan.added
            else "release private exclude claims"
        )
        typer.echo(f"  {action}: {plan.visibility_plan.exclude_path}")
    for item in plan.files:
        typer.echo(f"  {item.action.value}: {item.relative_destination}")


def _render_removal(plan: ProjectRemovePlan) -> None:
    typer.echo(f"project profile: {plan.profile}")
    typer.echo(f"target: {plan.target}")
    for item in plan.files:
        created = item.action is ProjectFileAction.CREATE
        if item.relative_destination in plan.git_removed or (
            created and not os.path.lexists(item.destination)
        ):
            typer.echo(f"  leave absent: {item.relative_destination}")
        elif created:
            typer.echo(f"  delete: {item.relative_destination}")
        else:
            typer.echo(f"  restore {item.action.value}: {item.relative_destination}")


def _render_stale_removal(plan: ProjectStaleRemovalPlan) -> None:
    typer.echo(f"project profile: {plan.profile}")
    typer.echo(f"target: {plan.target}")
    typer.echo(f"stale injection: {plan.reason}")
    for claim in plan.claims:
        typer.echo(f"  release ownership: {claim.resource_id.coordinate}")
    if plan.visibility_plan is not None:
        typer.echo(
            f"  release private exclude claims: {plan.visibility_plan.exclude_path}"
        )
    if plan.overlay_git_plan is not None:
        typer.echo(
            f"  release private filter claims: {plan.overlay_git_plan.attributes_path}"
        )
    typer.echo(
        "warning: the record's saved pre-injection contents are discarded and the "
        "injection cannot be removed normally afterwards; project files are left "
        "unchanged"
    )


def _render_sync(plan: ProjectSyncPlan) -> None:
    typer.echo(f"target: {plan.target}")
    typer.echo(
        "project profiles: " + ", ".join(item.profile for item in plan.injections)
    )
    for item in plan.files:
        status = item.kind.value
        if (
            item.kind.value == "update"
            and item.stored is not None
            and item.result.clean
            and not item.mode_conflict
            and item.result.merged() == item.live
            and item.result_mode == item.live_mode
            and item.desired_upstream == item.stored.upstream_payload
            and item.desired_mode == item.stored.upstream_mode
            and not item.restore_hidden_claim
        ):
            status = "unchanged"
        if item.restore_hidden_claim:
            status += " (restore private exclude claim)"
        if missing_locally(item):
            status = "missing locally (kept; --auto=use-profile restores it)"
        if not item.result.clean:
            conflicts = sum(
                isinstance(segment, Conflict) for segment in item.result.segments
            )
            status += f" ({conflicts} content conflict(s))"
        if item.mode_conflict:
            status += " (mode conflict)"
        typer.echo(f"  {item.profile}: {status}: {item.relative_destination}")


@project_app.command("list", epilog=PROJECT_LIST_EXAMPLES)
def project_list() -> None:
    """Show every recorded project injection and its actual visibility."""
    rows = list_projects()
    if not rows:
        typer.echo("no project injections recorded")
        return
    current: tuple[Path | None, str | None] | None = None
    failed = False
    for row in rows:
        group = (row.target, row.profile)
        if group != current:
            target = str(row.target) if row.target is not None else "unknown target"
            profile = row.profile or "unknown profile"
            typer.echo(f"{target}  [{profile}]")
            current = group
        if row.error is not None:
            failed = True
            destination = f"{row.destination}: " if row.destination is not None else ""
            typer.echo(f"  error: {destination}{row.error} ({row.record.name})")
        elif row.destination is None:
            typer.echo("  no files")
        else:
            assert row.visibility is not None
            typer.echo(f"  {row.visibility.value}: {row.destination}")
    if failed:
        raise typer.Exit(1)


@project_app.command("visibility", epilog=PROJECT_VISIBILITY_EXAMPLES)
def project_visibility(
    path: Path = typer.Argument(..., help="Existing project directory."),
    file: Path = typer.Argument(..., help="Target-relative injected destination."),
    hidden: bool = typer.Option(False, "--hidden", help="Keep this file private."),
    tracked: bool = typer.Option(
        False, "--tracked", help="Expose this file as ordinary Git content."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview without changing Git or private state."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Apply without an interactive prompt."
    ),
) -> None:
    """Change one injected destination's private Git visibility."""
    if hidden == tracked:
        raise SetforgeError("exactly one of --hidden or --tracked is required")
    requested = ProjectVisibility.HIDDEN if hidden else ProjectVisibility.TRACKED
    _convert_older_records(path, dry_run=dry_run)
    plan = plan_project_visibility(path, file, requested)
    typer.echo(f"target: {plan.target}")
    typer.echo(f"project profile: {plan.profile}")
    typer.echo(f"file: {plan.destination}")
    typer.echo(f"visibility: {plan.current.value} -> {requested.value}")
    if plan.git_dir is None:
        typer.echo("Git visibility: not applicable (target is not a Git worktree)")
        typer.echo("no changes: visibility is not applicable")
        return
    if not plan.changed:
        typer.echo("no changes: visibility is already current")
        return
    if dry_run:
        typer.echo("dry run: no changes applied")
        return
    if not _confirm("visibility", yes=yes):
        typer.echo("aborted: no changes applied")
        return
    apply_project_visibility(plan)
    typer.echo("visibility updated")


@project_app.command("inject", epilog=PROJECT_INJECT_EXAMPLES)
def project_inject(
    profile: str = typer.Argument(..., help="Project profile name."),
    path: Path = typer.Argument(..., help="Existing project directory."),
    config: Path = _CONFIG_OPTION,
    git_hidden: bool = typer.Option(
        False, "--git-hidden", help="Hide injected files with private Git excludes."
    ),
    git_tracked: bool = typer.Option(
        False, "--git-tracked", help="Leave injected files as normal Git content."
    ),
    auto: AutoResolution | None = typer.Option(
        None,
        "--auto",
        help="Resolve tracked-file conflicts with keep-live or use-profile.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview without changing files."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Apply without an interactive prompt."
    ),
) -> None:
    """Materialize a resolved project profile into PATH."""
    if git_hidden and git_tracked:
        raise SetforgeError("--git-hidden and --git-tracked are mutually exclusive")
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    resolved = resolve_project_profile(cfg, profile, config.parent)
    visibility = (
        ProjectVisibility.HIDDEN
        if git_hidden
        else ProjectVisibility.TRACKED
        if git_tracked
        else resolved.default_visibility
    )
    _convert_older_records(path, dry_run=dry_run)
    plan = plan_injection(
        profile=profile,
        target=path,
        config_root=config.parent,
        config_path=config,
        resolved=resolved,
        visibility=visibility,
    )
    _render_injection(plan)
    if dry_run:
        if not sys.stdin.isatty():
            # Without a TTY the apply cannot ask, so the preview must fail on
            # the same unresolved tracked-file conflict.
            resolve_injection_plan(plan, auto=auto, interactive=False)
        typer.echo("dry run: no changes applied")
        return
    if not _confirm("inject", yes=yes):
        typer.echo("aborted: no changes applied")
        return
    resolved_plan = resolve_injection_plan(
        plan, auto=auto, interactive=sys.stdin.isatty()
    )
    if resolved_plan is None:
        typer.echo("aborted: no changes applied")
        return
    apply_injection(resolved_plan)
    typer.echo("injection complete")


@project_app.command("sync", epilog=PROJECT_SYNC_EXAMPLES)
def project_sync(
    path: Path = typer.Argument(..., help="Existing project directory."),
    auto: AutoResolution | None = typer.Option(
        None,
        "--auto",
        help="Resolve every conflict with keep-live or use-profile.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview without changing files or private state."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Apply without an interactive prompt."
    ),
) -> None:
    """Synchronize every recorded profile injection at PATH atomically."""
    _convert_older_records(path, dry_run=dry_run)
    plan = plan_sync(path)
    _render_sync(plan)
    if dry_run:
        typer.echo("dry run: no changes applied")
        return
    if not _confirm("sync", yes=yes):
        typer.echo("aborted: no changes applied")
        return
    resolved = resolve_sync_plan(
        plan,
        auto=auto,
        interactive=sys.stdin.isatty(),
    )
    if resolved is None:
        typer.echo("aborted: unresolved project sync; no changes applied")
        raise typer.Exit(1)
    changed = apply_sync(resolved)
    typer.echo("sync complete" if changed else "no changes: project is already current")


@project_app.command("remove", epilog=PROJECT_REMOVE_EXAMPLES)
def project_remove(
    profile: str = typer.Argument(..., help="Project profile name."),
    path: Path = typer.Argument(..., help="Existing project directory."),
    config: Path = _CONFIG_OPTION,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview without changing files."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Apply without an interactive prompt."
    ),
) -> None:
    """Restore the exact state that preceded one injection.

    When the project directory was moved, deleted, or replaced, or the
    injection record was lost, this drops the leftover record, ownership
    claims, and private Git entries instead and leaves project files alone.
    """
    config = _resolve_config_arg(config)
    _convert_older_records(path, dry_run=dry_run)
    stale = plan_stale_removal(profile=profile, target=path, config_path=config)
    if stale is not None:
        _render_stale_removal(stale)
        if dry_run:
            typer.echo("dry run: no changes applied")
            return
        if not _confirm("remove", yes=yes):
            typer.echo("aborted: no changes applied")
            return
        apply_stale_removal(stale)
        typer.echo("stale injection dropped")
        return
    plan = plan_removal(profile=profile, target=path, config_path=config)
    _render_removal(plan)
    if dry_run:
        typer.echo("dry run: no changes applied")
        return
    if not _confirm("remove", yes=yes):
        typer.echo("aborted: no changes applied")
        return
    apply_removal(plan)
    typer.echo("removal complete")
