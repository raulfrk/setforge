"""CLI for reversible project-profile injection, sync, and removal."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import (
    PROJECT_INJECT_EXAMPLES,
    PROJECT_REMOVE_EXAMPLES,
    PROJECT_SYNC_EXAMPLES,
)
from setforge.config import ProjectVisibility, load_config, resolve_project_profile
from setforge.errors import ConfirmRequiresInteractive, SetforgeError
from setforge.project_injection import (
    ProjectInjectionPlan,
    ProjectRemovePlan,
    apply_injection,
    apply_removal,
    plan_injection,
    plan_removal,
    resolve_injection_plan,
)
from setforge.project_overlay import process_filter
from setforge.project_sync import (
    AutoResolution,
    ProjectSyncPlan,
    apply_sync,
    plan_sync,
    resolve_sync_plan,
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
        typer.echo(f"  restore {item.action.value}: {item.relative_destination}")
    typer.echo("worktree auto-carry hook: unchanged")


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
            and not item.legacy
            and item.result.clean
            and not item.mode_conflict
            and item.result.merged() == item.live
            and item.result_mode == item.live_mode
            and item.desired_upstream == item.stored.upstream_payload
            and item.desired_mode == item.stored.upstream_mode
        ):
            status = "unchanged"
        if not item.result.clean:
            conflicts = sum(
                isinstance(segment, Conflict) for segment in item.result.segments
            )
            status += f" ({conflicts} content conflict(s))"
        if item.mode_conflict:
            status += " (mode conflict)"
        if item.legacy:
            status += " (legacy two-way)"
        typer.echo(f"  {item.profile}: {status}: {item.relative_destination}")


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
    plan = plan_injection(
        profile=profile,
        target=path,
        config_root=config.parent,
        config_path=config,
        resolved=resolved,
        visibility=visibility,
    )
    if plan.no_op:
        changed = apply_injection(plan, mutate_visibility=not dry_run)
        _render_injection(plan)
        if dry_run:
            typer.echo("dry run: no changes applied")
        else:
            typer.echo(
                "visibility activated for the existing injection"
                if changed
                else "no changes: this exact injection is already current"
            )
        return
    _render_injection(plan)
    if dry_run:
        typer.echo("dry run: no changes applied")
        return
    if not _confirm("inject", yes=yes):
        typer.echo("aborted: no changes applied")
        return
    resolved_plan = resolve_injection_plan(
        plan,
        auto=auto.value if auto is not None else None,
        interactive=sys.stdin.isatty(),
    )
    if resolved_plan is None:
        typer.echo("aborted: no changes applied")
        return
    changed = apply_injection(resolved_plan)
    typer.echo(
        "injection complete" if changed else "no changes: injection already current"
    )


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
    """Restore the exact state that preceded one injection."""
    config = _resolve_config_arg(config)
    plan = plan_removal(profile=profile, target=path, config_root=config.parent)
    _render_removal(plan)
    if dry_run:
        typer.echo("dry run: no changes applied")
        return
    if not _confirm("remove", yes=yes):
        typer.echo("aborted: no changes applied")
        return
    apply_removal(plan, config_root=config.parent.resolve(strict=True))
    typer.echo("removal complete")
