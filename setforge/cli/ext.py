"""ext subcommand group — manage VSCode extensions in setforge.yaml.

``ext list`` / ``add`` / ``remove`` / ``reconcile`` operate on the
``extensions`` block of the resolved profile, optionally invoking ``code
--install-extension`` / ``--uninstall-extension`` via :mod:`vscode_extensions`.
"""

from pathlib import Path

import typer

from setforge import vscode_extensions
from setforge.cli import _CONFIG_OPTION, _PROFILE_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import (
    EXT_ADD_EXAMPLES,
    EXT_LIST_EXAMPLES,
    EXT_RECONCILE_EXAMPLES,
    EXT_REMOVE_EXAMPLES,
)
from setforge.config import ReconcilePolicy, load_config, resolve_profile
from setforge.errors import ExtensionInstallFailed, ExtensionToolMissing

ext_app: typer.Typer = typer.Typer(
    help="Manage VSCode extensions in setforge.yaml.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(ext_app, name="ext")


@ext_app.command("list", epilog=EXT_LIST_EXAMPLES)
def ext_list(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Show declared (YAML) vs installed (code --list-extensions)."""
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    resolved = resolve_profile(cfg, profile)
    declared_include = set(resolved.extensions.include)
    declared_exclude = set(resolved.extensions.exclude)

    try:
        installed = vscode_extensions.list_installed()
    except ExtensionToolMissing as exc:
        typer.secho(f"warning: {exc}", err=True, fg=typer.colors.YELLOW)
        installed = set()

    all_ids = sorted(declared_include | declared_exclude | installed)
    if not all_ids:
        typer.echo("(no extensions declared or installed)")
        return

    width = max(len(ext_id) for ext_id in all_ids) + 2
    typer.echo(f"{'extension':<{width}}{'declared':<12}{'installed':<10}")
    for ext_id in all_ids:
        if ext_id in declared_exclude:
            declared = "exclude"
        elif ext_id in declared_include:
            declared = "include"
        else:
            declared = "-"
        is_installed = "yes" if ext_id in installed else "no"
        typer.echo(f"{ext_id:<{width}}{declared:<12}{is_installed:<10}")


@ext_app.command("add", epilog=EXT_ADD_EXAMPLES)
def ext_add(
    extension_id: str = typer.Argument(..., help="VSCode extension ID."),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    install: bool = typer.Option(
        True,
        "--install/--no-install",
        help="Run code --install-extension after editing YAML.",
    ),
) -> None:
    """Append an extension ID to the profile's extensions.include list."""
    config = _resolve_config_arg(config)
    added = vscode_extensions.add_to_include(config, profile, extension_id)
    if added:
        typer.echo(f"added to {profile}.extensions.include: {extension_id}")
    else:
        typer.echo(f"already in {profile}.extensions.include: {extension_id}")
    if install:
        try:
            vscode_extensions.install_one(extension_id)
            typer.echo(f"installed  {extension_id}")
        except ExtensionToolMissing as exc:
            typer.secho(
                f"warning: skipping install — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
        except ExtensionInstallFailed as exc:
            typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc


@ext_app.command("remove", epilog=EXT_REMOVE_EXAMPLES)
def ext_remove(
    extension_id: str = typer.Argument(..., help="VSCode extension ID."),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    exclude: bool = typer.Option(
        False,
        "--exclude",
        help="Also add to extensions.exclude so reconcile actively uninstalls.",
    ),
) -> None:
    """Remove an extension ID from the profile's extensions.include list."""
    config = _resolve_config_arg(config)
    changed = vscode_extensions.remove_from_include(
        config, profile, extension_id, add_to_exclude_list=exclude
    )
    if changed:
        target = "include + exclude" if exclude else "include"
        typer.echo(f"updated {profile}.extensions.{target}: {extension_id}")
    else:
        typer.echo(f"no change: {extension_id} not in include list")


@ext_app.command("reconcile", epilog=EXT_RECONCILE_EXAMPLES)
def ext_reconcile(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute actions without invoking code CLI."
    ),
) -> None:
    """Run reconcile explicitly (in addition to the install loop).

    Exits non-zero on non-empty drift when policy is REPORT or
    ``--dry-run`` is set — both are read-only modes intended for CI.
    A live run (the policy actually applies changes) also exits non-zero
    when it has failed actions (``report.failed``).
    """
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    resolved = resolve_profile(cfg, profile)
    try:
        report = vscode_extensions.reconcile(resolved.extensions, dry_run=dry_run)
    except ExtensionToolMissing as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    is_read_only = resolved.extensions.reconcile is ReconcilePolicy.REPORT or dry_run

    failed_ids = {ext_id for ext_id, _ in report.failed}
    for ext_id in report.to_install:
        verb = "would install" if is_read_only else "install"
        if ext_id not in failed_ids:
            typer.echo(f"{verb}    {ext_id}")
    for ext_id in report.to_uninstall:
        verb = "would uninstall" if is_read_only else "uninstall"
        if ext_id not in failed_ids:
            typer.echo(f"{verb}  {ext_id}")
    for ext_id, err in report.failed:
        typer.secho(f"FAILED   {ext_id} — {err}", err=True, fg=typer.colors.YELLOW)
    if not report:
        typer.echo("nothing to reconcile")
    elif is_read_only:  # noqa: SIM114 — read-only drift and live-run failure are distinct exit conditions; keep branches separate
        raise typer.Exit(code=1)
    elif report.failed:
        raise typer.Exit(code=1)
