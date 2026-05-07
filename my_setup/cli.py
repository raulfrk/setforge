"""Typer CLI entry point for ``my-setup``.

Commands wired in Pillar 1: ``install``, ``compare``, ``capture``, ``sync``.
Pillar 2 adds extension reconcile inside ``install``. Claude plugin
reconcile lands in Pillar 3.
"""

import logging
import sys
from pathlib import Path

import typer

from my_setup import capture as capture_mod
from my_setup import compare as compare_mod
from my_setup import deploy
from my_setup import extensions as extensions_mod
from my_setup.compare import expand_dotfile, resolve_dst, resolve_src
from my_setup.config import ReconcilePolicy, load_config, resolve_profile
from my_setup.errors import ExtensionToolMissing, MySetupError

LOGGER = logging.getLogger(__name__)

app = typer.Typer(
    help="my-setup: dotfile + extension + Claude-plugin orchestration.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


_CONFIG_OPTION = typer.Option(
    Path("my_setup.yaml"),
    "--config",
    "-c",
    help="Path to my_setup.yaml.",
    show_default=True,
)
_PROFILE_OPTION = typer.Option(
    ...,
    "--profile",
    "-p",
    help="Profile name from my_setup.yaml.",
)


@app.command()
def install(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Deploy tracked → live for every dotfile in the profile."""
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)

    deploy.validate_srcs_exist(cfg, resolved, repo_root)
    deploy.bootstrap_local(resolved.bootstrap)

    for name in resolved.dotfiles:
        dotfile = cfg.dotfiles[name]
        src = resolve_src(dotfile, repo_root)
        dst = resolve_dst(dotfile)
        for _, sub_src, sub_dst in expand_dotfile(name, src, dst):
            result = deploy.copy_atomic(
                sub_src,
                sub_dst,
                preserve_user_sections=dotfile.preserve_user_sections,
                preserve_user_keys=dotfile.preserve_user_keys or None,
            )
            typer.echo(f"{result.action.value:>8}  {sub_dst}")

    try:
        report = extensions_mod.reconcile(resolved.extensions)
    except ExtensionToolMissing as exc:
        typer.secho(
            f"warning: skipping extension reconcile — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return

    failed_ids = {ext_id for ext_id, _ in report.failed}
    for ext_id in report.to_install:
        if ext_id not in failed_ids:
            typer.echo(f"installed  {ext_id}")
    for ext_id in report.to_uninstall:
        if ext_id not in failed_ids:
            typer.echo(f"uninstalled  {ext_id}")
    for ext_id, err in report.failed:
        typer.secho(
            f"FAILED  {ext_id} — {err}", err=True, fg=typer.colors.YELLOW
        )
    if not report:
        typer.echo("extensions: nothing to reconcile")


@app.command()
def compare(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    full: bool = typer.Option(
        False, "--full", help="Print unified diff body for drifted entries."
    ),
    check: bool = typer.Option(
        False, "--check", help="Exit non-zero on unexpected drift (for CI)."
    ),
) -> None:
    """Report drift between tracked and live for every dotfile in the profile."""
    cfg = load_config(config)
    repo_root = config.resolve().parent
    report = compare_mod.compare_profile(cfg, profile, repo_root)

    for entry in report.entries:
        line = f"{entry.status.value:>10}  {entry.name}"
        if entry.expected_drift_keys or entry.unexpected_drift_keys:
            line += (
                f"  (expected={len(entry.expected_drift_keys)},"
                f" unexpected={len(entry.unexpected_drift_keys)})"
            )
        typer.echo(line)
        if full and entry.diff:
            typer.echo(entry.diff)

    if check and report.has_unexpected_drift:
        raise typer.Exit(code=1)


@app.command()
def capture(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Capture live → tracked for every dotfile in the profile."""
    cfg = load_config(config)
    repo_root = config.resolve().parent
    results = capture_mod.capture_profile(cfg, profile, repo_root)
    for result in results:
        typer.echo(f"{result.action.value:>8}  {result.name}")


@app.command()
def sync(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Capture live → tracked for dotfiles and extensions."""
    capture(profile=profile, config=config)
    try:
        changed = extensions_mod.capture_extensions(config, profile)
    except ExtensionToolMissing as exc:
        typer.secho(
            f"warning: skipping extension capture — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return
    typer.echo(
        f"extensions: include {'updated' if changed else 'unchanged'}"
    )


ext_app = typer.Typer(
    help="Manage VSCode extensions in my_setup.yaml.",
    no_args_is_help=True,
)
app.add_typer(ext_app, name="ext")


@ext_app.command("list")
def ext_list(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Show declared (YAML) vs installed (code --list-extensions)."""
    cfg = load_config(config)
    resolved = resolve_profile(cfg, profile)
    declared_include = set(resolved.extensions.include)
    declared_exclude = set(resolved.extensions.exclude)

    try:
        installed = extensions_mod.list_installed()
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


@ext_app.command("add")
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
    added = extensions_mod.add_to_include(config, profile, extension_id)
    if added:
        typer.echo(f"added to {profile}.extensions.include: {extension_id}")
    else:
        typer.echo(
            f"already in {profile}.extensions.include: {extension_id}"
        )
    if install:
        try:
            extensions_mod.install_one(extension_id)
            typer.echo(f"installed  {extension_id}")
        except ExtensionToolMissing as exc:
            typer.secho(
                f"warning: skipping install — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )


@ext_app.command("remove")
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
    changed = extensions_mod.remove_from_include(
        config, profile, extension_id, add_to_exclude_list=exclude
    )
    if changed:
        target = "include + exclude" if exclude else "include"
        typer.echo(f"updated {profile}.extensions.{target}: {extension_id}")
    else:
        typer.echo(f"no change: {extension_id} not in include list")


@ext_app.command("reconcile")
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
    """
    cfg = load_config(config)
    resolved = resolve_profile(cfg, profile)
    try:
        report = extensions_mod.reconcile(resolved.extensions, dry_run=dry_run)
    except ExtensionToolMissing as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    is_read_only = (
        resolved.extensions.reconcile is ReconcilePolicy.REPORT or dry_run
    )

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
        typer.secho(
            f"FAILED   {ext_id} — {err}", err=True, fg=typer.colors.YELLOW
        )
    if not report:
        typer.echo("nothing to reconcile")
    elif is_read_only:
        raise typer.Exit(code=1)


def main() -> None:
    """Entry point that wraps ``app`` with :class:`MySetupError` handling."""
    try:
        app()
    except MySetupError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        sys.exit(1)


if __name__ == "__main__":
    main()
