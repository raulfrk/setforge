"""CLI for reversible project-profile injection and removal."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import (
    PROJECT_INJECT_EXAMPLES,
    PROJECT_REMOVE_EXAMPLES,
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
)

project_app = typer.Typer(
    help="Inject and remove reusable files in a project worktree.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(project_app, name="project")


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
    typer.echo(f"Git visibility: {plan.visibility.value}")
    if plan.visibility_plan.changed:
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


@project_app.command("inject", epilog=PROJECT_INJECT_EXAMPLES)
def project_inject(
    profile: str = typer.Argument(..., help="Project profile name."),
    path: Path = typer.Argument(..., help="Existing Git worktree root."),
    config: Path = _CONFIG_OPTION,
    git_hidden: bool = typer.Option(
        False, "--git-hidden", help="Hide injected files with private Git excludes."
    ),
    git_tracked: bool = typer.Option(
        False, "--git-tracked", help="Leave injected files as normal Git content."
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
    changed = apply_injection(plan)
    typer.echo(
        "injection complete" if changed else "no changes: injection already current"
    )


@project_app.command("remove", epilog=PROJECT_REMOVE_EXAMPLES)
def project_remove(
    profile: str = typer.Argument(..., help="Project profile name."),
    path: Path = typer.Argument(..., help="Existing Git worktree root."),
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
