"""Typer CLI entry point for ``setforge``.

Commands wired in Pillar 1: ``install``, ``compare``, ``capture``, ``sync``.
Pillar 2 adds extension reconcile inside ``install``. Claude plugin
reconcile lands in Pillar 3. ``revert`` (setforge-19n) replays the most
recent transition for a profile in reverse.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

import typer

from setforge import (
    binaries,
    vscode_extensions,
)
from setforge import claude_plugins as claude_plugins_mod
from setforge import source as source_mod
from setforge.config import (
    ClaudeInstallMode,
    Config,
    ReconcilePolicy,
    load_config,
    resolve_profile,
)
from setforge.errors import (
    ExtensionToolMissing,
    MarketplaceCacheMiss,
    PluginToolMissing,
    SetforgeError,
)

LOGGER = logging.getLogger(__name__)

app = typer.Typer(
    help="setforge: tracked file + extension + Claude plugin orchestration.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


_CONFIG_OPTION = typer.Option(
    Path("setforge.yaml"),
    "--config",
    "-c",
    help="Path to setforge.yaml.",
    show_default=True,
)
_PROFILE_OPTION = typer.Option(
    ...,
    "--profile",
    "-p",
    help="Profile name from setforge.yaml.",
)
_SOURCE_OPTION = typer.Option(
    None,
    source_mod.CLI_FLAG,
    help="Path to a config source directory (containing setforge.yaml). "
    "Takes precedence over SETFORGE_SOURCE and "
    "~/.config/setforge/local.yaml `source:` block. Paths only — git "
    "sources live in local.yaml. The per-command --config flag, when "
    "set explicitly, overrides this; the source-layer discovery only "
    "fires when --config is left at its default AND the CWD has no "
    "setforge.yaml.",
)


def _resolve_config_arg(config: Path) -> Path:
    """Resolve a command's ``--config`` arg through the source-layer fallback.

    Precedence:

    1. ``--config`` explicitly set (non-default) → use it (legacy flow).
    2. ``--config`` at its default → consult the source-layer (``--source``
       > ``SETFORGE_SOURCE`` > ``~/.config/setforge/local.yaml`` > CWD
       fallback), then return the ``setforge.yaml`` inside the resolved
       source dir.

    The 4th tier of the source-layer (CWD-fallback) preserves the legacy
    "run from inside config repo" UX bit-for-bit: when no source layer
    is configured, ``setforge install`` from a CWD containing
    ``setforge.yaml`` still works without ``--config``.
    """
    default = Path("setforge.yaml")
    if config != default:
        return config
    resolved_source = source_mod.get_resolved_source()
    return source_mod.validate_source_dir(resolved_source)


@app.callback()
def _root(
    code_bin: str | None = typer.Option(
        None,
        "--code-bin",
        help="Override path to the 'code' (VSCode) binary. "
        "Takes precedence over SETFORGE_CODE_BIN and ~/.config/setforge/local.yaml.",
    ),
    claude_bin: str | None = typer.Option(
        None,
        "--claude-bin",
        help="Override path to the 'claude' binary. "
        "Takes precedence over SETFORGE_CLAUDE_BIN and ~/.config/setforge/local.yaml.",
    ),
    patch_bin: str | None = typer.Option(
        None,
        "--patch-bin",
        help="Override path to the GNU 'patch' binary. "
        "Takes precedence over SETFORGE_PATCH_BIN and ~/.config/setforge/local.yaml.",
    ),
    source: Path | None = _SOURCE_OPTION,
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Emit DEBUG-level logging to stderr.",
    ),
) -> None:
    """Wire host-local binary overrides, configure logging, ensure local stub exists."""
    if verbose:
        level = logging.DEBUG
    else:
        env_value = os.environ.get("SETFORGE_LOG_LEVEL", "WARNING")
        # `getattr(logging, "Logger", None)` returns the Logger class, not None;
        # restrict to known int level constants so a non-level module attribute
        # falls back to WARNING instead of crashing inside basicConfig.
        resolved = getattr(logging, env_value.upper(), None)
        if not isinstance(resolved, int):
            sys.stderr.write(
                f"setforge: unknown SETFORGE_LOG_LEVEL={env_value!r}; "
                f"defaulting to WARNING\n"
            )
            level = logging.WARNING
        else:
            level = resolved
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        force=True,
    )
    LOGGER.debug("logging configured at level %s", logging.getLevelName(level))
    binaries.set_cli_overrides(code=code_bin, claude=claude_bin, patch=patch_bin)
    binaries.ensure_local_config_stub()
    source_mod.set_cli_source(source)


ext_app = typer.Typer(
    help="Manage VSCode extensions in setforge.yaml.",
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
    changed = vscode_extensions.remove_from_include(
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
    elif is_read_only:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# plugin sub-app
# ---------------------------------------------------------------------------

plugin_app = typer.Typer(
    help="Manage Claude plugins in setforge.yaml.",
    no_args_is_help=True,
)
app.add_typer(plugin_app, name="plugin")


@plugin_app.command("list")
def plugin_list(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Show declared (YAML) vs installed (claude plugin list) status."""
    cfg = load_config(config)
    resolved = resolve_profile(cfg, profile)
    declared_ids = set(resolved.claude_plugins)

    try:
        installed = claude_plugins_mod.list_installed()
    except PluginToolMissing as exc:
        typer.secho(f"warning: {exc}", err=True, fg=typer.colors.YELLOW)
        installed = {}

    # Columns: Declared | Installed | Status
    # Status values:  enabled / disabled / missing-from-decl / missing-from-install
    all_ids = sorted(declared_ids | set(installed))
    if not all_ids:
        typer.echo("(no plugins declared or installed)")
        return

    width = max(len(pid) for pid in all_ids) + 2
    typer.echo(f"{'plugin':<{width}}{'declared':<12}{'status':<22}")
    for pid in all_ids:
        is_declared = "yes" if pid in declared_ids else "no"
        if pid in installed:
            status = "enabled" if installed[pid].get("enabled", True) else "disabled"
        elif pid in declared_ids:
            status = "missing-from-install"
        else:
            status = "missing-from-decl"
        typer.echo(f"{pid:<{width}}{is_declared:<12}{status:<22}")


@plugin_app.command("add")
def plugin_add(
    name: str = typer.Argument(
        ...,
        help=(
            "Plugin name (in <name>@<marketplace> form or just <name>"
            " with --marketplace)."
        ),
    ),
    from_: str = typer.Option(
        ...,
        "--from",
        help="Marketplace source: 'github:owner/repo' or 'path:/local/dir'.",
    ),
    marketplace: str | None = typer.Option(
        None,
        "--marketplace",
        "-m",
        help="Marketplace name to install the plugin from (when name is bare).",
    ),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    no_install: bool = typer.Option(
        False,
        "--no-install",
        help="Register in YAML only; skip `claude plugin install`.",
    ),
) -> None:
    """Register a marketplace (if new), declare plugin in YAML, and install."""
    # Parse plugin name and marketplace from the argument
    if "@" in name:
        plugin_name, mp_name = name.split("@", 1)
    elif marketplace:
        plugin_name = name
        mp_name = marketplace
    else:
        typer.secho(
            "error: provide plugin as <name>@<marketplace> or use --marketplace",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    load_config(config)

    # Parse --from into a MarketplaceSource
    from setforge.config import MarketplaceSource, MarketplaceSourceKind

    if from_.startswith("github:"):
        repo = from_[len("github:") :]
        source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo=repo)
    elif from_.startswith("path:"):
        local_path = Path(from_[len("path:") :]).expanduser()
        source = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=local_path)
    else:
        typer.secho(
            f"error: unrecognised --from format {from_!r};"
            " use github:owner/repo or path:/dir",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    # Register marketplace in YAML if not already present
    mp_added = claude_plugins_mod.yaml_add_marketplace(config, mp_name, source)
    if mp_added:
        typer.echo(f"registered marketplace: {mp_name}")
        # Add marketplace to claude via CLI
        try:
            claude_plugins_mod.marketplace_add(mp_name, source)
            typer.echo(f"marketplace added: {mp_name}")
        except PluginToolMissing as exc:
            typer.secho(f"warning: {exc}", err=True, fg=typer.colors.YELLOW)

    # Declare plugin in top-level claude_plugins block
    plugin_declared = claude_plugins_mod.yaml_add_plugin(config, plugin_name, mp_name)
    if plugin_declared:
        typer.echo(f"declared plugin: {plugin_name} @ {mp_name}")

    # Add to profile
    profile_added = claude_plugins_mod.yaml_add_plugin_to_profile(
        config, profile, f"{plugin_name}@{mp_name}"
    )
    if profile_added:
        typer.echo(f"added to {profile}.claude_plugins: {plugin_name}@{mp_name}")

    # Install via claude CLI, then strictly enable. The enable step is
    # required because `claude plugin install` writes
    # ``installed_plugins.json`` without flipping ``enabledPlugins`` —
    # without this second call the plugin lands disabled (see
    # setforge-l37). Strict failure on enable matches the interactive
    # single-plugin shape of `plugin add`: a silent warning would be a
    # footgun. The install half retains today's pattern; latent
    # subprocess-error handling on install is tracked separately as
    # setforge-oyv.
    if not no_install:
        try:
            claude_plugins_mod.plugin_install(plugin_name, mp_name)
        except PluginToolMissing as exc:
            typer.secho(
                f"warning: skipping install — {exc}", err=True, fg=typer.colors.YELLOW
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            typer.secho(
                f"ERROR: install failed — {binaries.stderr_of(exc)}",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1) from exc
        else:
            typer.echo(f"installed plugin: {plugin_name}@{mp_name}")
            try:
                claude_plugins_mod.plugin_enable(f"{plugin_name}@{mp_name}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                typer.secho(
                    f"ERROR: enable failed — {binaries.stderr_of(exc)}",
                    err=True,
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=1) from exc
            typer.echo(f"enabled plugin: {plugin_name}@{mp_name}")


@plugin_app.command("remove")
def plugin_remove(
    name: str = typer.Argument(..., help="Plugin name (bare or <name>@<marketplace>)."),
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    disable: bool = typer.Option(
        False,
        "--disable",
        help="Also run `claude plugin disable` after removing from YAML.",
    ),
) -> None:
    """Remove a plugin from the profile's claude_plugins list."""
    plugin_ref = name  # already in <name>@<marketplace> form or just name
    changed = claude_plugins_mod.yaml_remove_plugin_from_profile(
        config, profile, plugin_ref
    )
    if changed:
        typer.echo(f"removed from {profile}.claude_plugins: {plugin_ref}")
    else:
        typer.echo(f"not in {profile}.claude_plugins: {plugin_ref}")
    if disable:
        try:
            claude_plugins_mod.plugin_disable(plugin_ref)
            typer.echo(f"disabled plugin: {plugin_ref}")
        except PluginToolMissing as exc:
            typer.secho(f"warning: {exc}", err=True, fg=typer.colors.YELLOW)


@plugin_app.command("reconcile")
def plugin_reconcile(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute actions without calling claude CLI."
    ),
) -> None:
    """Explicit reconcile (in addition to the automatic run inside install).

    Exits non-zero when policy is REPORT or --dry-run and there is drift.
    """
    cfg = load_config(config)
    resolved = resolve_profile(cfg, profile)
    try:
        report = claude_plugins_mod.reconcile(cfg, resolved, dry_run=dry_run)
    except PluginToolMissing as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    is_read_only = resolved.plugins_reconcile is ReconcilePolicy.REPORT or dry_run
    failed_ids = {pid for pid, _ in report.failed}

    for name, mp in report.to_install:
        pid = f"{name}@{mp}"
        verb = "would install" if is_read_only else "installed"
        if pid not in failed_ids:
            typer.echo(f"{verb}  {pid}")
    for pid in report.to_enable:
        verb = "would enable" if is_read_only else "enabled"
        if pid not in failed_ids:
            typer.echo(f"{verb}   {pid}")
    for pid in report.to_disable:
        verb = "would disable" if is_read_only else "disabled"
        if pid not in failed_ids:
            typer.echo(f"{verb}  {pid}")
    for pid, err in report.failed:
        typer.secho(f"FAILED  {pid} — {err}", err=True, fg=typer.colors.YELLOW)
    if not report:
        typer.echo("plugins: nothing to reconcile")
    elif is_read_only:
        raise typer.Exit(code=1)


@plugin_app.command("sync-cache")
def sync_cache(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Clone/refresh marketplace caches for offline-capable install.

    Required when ``~/.config/setforge/local.yaml`` sets
    ``claude.install_mode: local-clone``. Iterates every GitHub-backed
    ``MarketplaceSource`` referenced by ``profile`` and either clones
    it into ``~/.cache/setforge/marketplaces/<name>`` (if absent) or
    fetches + hard-resets it to ``origin/HEAD`` (if present). When
    ``install_mode`` is ``regular``, prints a warning and exits 0
    without touching the cache.
    """
    host_local = binaries.load_host_local_config()
    if host_local.claude.install_mode is ClaudeInstallMode.REGULAR:
        typer.secho(
            "warning: claude.install_mode is 'regular'; sync-cache is only "
            "useful when local-clone is active. Set claude.install_mode: "
            "local-clone in ~/.config/setforge/local.yaml to opt in.",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return

    cfg = load_config(config)
    resolved = resolve_profile(cfg, profile)
    try:
        refreshed = claude_plugins_mod.sync_marketplace_cache(cfg, resolved)
    except MarketplaceCacheMiss as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if not refreshed:
        typer.echo("sync-cache: no GitHub-backed marketplaces in profile")
        return
    for mp in refreshed:
        typer.echo(f"sync-cache: refreshed {mp}")


# ---------------------------------------------------------------------------
# marketplace sub-app
# ---------------------------------------------------------------------------

marketplace_app = typer.Typer(
    help="Manage Claude plugin marketplaces in setforge.yaml.",
    no_args_is_help=True,
)
app.add_typer(marketplace_app, name="marketplace")


@marketplace_app.command("add")
def marketplace_add_cmd(
    name: str = typer.Argument(..., help="Marketplace name."),
    from_: str = typer.Option(
        ...,
        "--from",
        help="Source: 'github:owner/repo' or 'path:/local/dir'.",
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Register a marketplace in YAML and run claude plugin marketplace add."""
    from setforge.config import MarketplaceSource, MarketplaceSourceKind

    if from_.startswith("github:"):
        repo = from_[len("github:") :]
        source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo=repo)
    elif from_.startswith("path:"):
        local_path = Path(from_[len("path:") :]).expanduser()
        source = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=local_path)
    else:
        typer.secho(
            f"error: unrecognised --from format {from_!r};"
            " use github:owner/repo or path:/dir",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    yaml_changed = claude_plugins_mod.yaml_add_marketplace(config, name, source)
    if yaml_changed:
        typer.echo(f"added {name} to marketplaces in YAML")
    else:
        typer.echo(f"marketplace already declared: {name}")

    try:
        claude_plugins_mod.marketplace_add(name, source)
        typer.echo(f"registered marketplace: {name}")
    except PluginToolMissing as exc:
        typer.secho(f"warning: {exc}", err=True, fg=typer.colors.YELLOW)


@marketplace_app.command("remove")
def marketplace_remove_cmd(
    name: str = typer.Argument(..., help="Marketplace name."),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Remove a marketplace from YAML and run claude plugin marketplace remove."""
    yaml_changed = claude_plugins_mod.yaml_remove_marketplace(config, name)
    if yaml_changed:
        typer.echo(f"removed {name} from marketplaces in YAML")
    else:
        typer.echo(f"marketplace not found in YAML: {name}")

    try:
        claude_plugins_mod.marketplace_remove(name)
        typer.echo(f"removed marketplace: {name}")
    except PluginToolMissing as exc:
        typer.secho(f"warning: {exc}", err=True, fg=typer.colors.YELLOW)


@marketplace_app.command("update")
def marketplace_update_cmd(
    name: str = typer.Argument(..., help="Marketplace name."),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Run claude plugin marketplace update for a named marketplace."""
    try:
        claude_plugins_mod.marketplace_update(name)
        typer.echo(f"updated marketplace: {name}")
    except PluginToolMissing as exc:
        typer.secho(f"warning: {exc}", err=True, fg=typer.colors.YELLOW)


def _check_profile(
    cfg: Config,
    prof_name: str,
    repo_root: Path,
    failures: list[str],
) -> None:
    """Run checks 2-6 for a single profile, appending failures in-place."""
    from jinja2 import StrictUndefined, Template, TemplateSyntaxError, UndefinedError

    from setforge.compare import resolve_src
    from setforge.paths import template_context

    ctx = f"profile {prof_name!r}"

    # Check 2: Profile resolution (covers missing profiles + cycle detection).
    try:
        resolved = resolve_profile(cfg, prof_name)
    except SetforgeError as exc:
        failures.append(f"{ctx}: {exc}")
        return

    for tracked_file_name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[tracked_file_name]
        dot_ctx = f"{ctx}: tracked_file {tracked_file_name!r}"

        # Check 3: Jinja2 dst template renderability (StrictUndefined catches typos).
        if tracked_file.template:
            try:
                Template(tracked_file.dst, undefined=StrictUndefined).render(
                    **template_context()
                )
            except (TemplateSyntaxError, UndefinedError) as exc:
                failures.append(f"{dot_ctx}: unrenderable dst template: {exc}")
                continue

        # Check 4: tracked src exists on disk.
        src = resolve_src(tracked_file, repo_root)
        if not src.exists():
            failures.append(f"{dot_ctx}: src {tracked_file.src} does not exist")

    # Check 5: extension include list — non-empty IDs, no duplicates.
    # Check the raw profile (before extends-merging) so duplicates that
    # _merge_list would silently drop are still caught here.
    raw_include = cfg.profiles[prof_name].extensions.include
    seen_ext: set[str] = set()
    reported_dup_ext: set[str] = set()
    empty_reported_ext = False
    for ext_id in raw_include:
        if not ext_id.strip():
            if not empty_reported_ext:
                failures.append(f"{ctx}: extensions.include contains empty ID")
                empty_reported_ext = True
        elif ext_id in seen_ext:
            if ext_id not in reported_dup_ext:
                failures.append(f"{ctx}: extensions.include duplicate: {ext_id!r}")
                reported_dup_ext.add(ext_id)
        else:
            seen_ext.add(ext_id)

    # Same raw-profile rationale as Check 5: _merge_list dedupes during
    # resolve_profile, so duplicates would be silently swallowed by the
    # resolved list. Walk the raw list to catch them at config time.
    raw_plugins = cfg.profiles[prof_name].claude_plugins
    seen_plugin: set[str] = set()
    reported_dup_plugin: set[str] = set()
    empty_reported_plugin = False
    for plugin_ref in raw_plugins:
        if not plugin_ref.strip():
            if not empty_reported_plugin:
                failures.append(f"{ctx}: claude_plugins contains empty ref")
                empty_reported_plugin = True
        elif plugin_ref in seen_plugin:
            if plugin_ref not in reported_dup_plugin:
                failures.append(f"{ctx}: claude_plugins duplicate: {plugin_ref!r}")
                reported_dup_plugin.add(plugin_ref)
        else:
            seen_plugin.add(plugin_ref)

    # Check 6: claude_plugins marketplace-reference internal consistency.
    # Every plugin referenced in the profile must have its marketplace
    # declared in cfg.marketplaces. (Plugin existence in cfg.claude_plugins
    # is already validated by load_config → _validate_plugin_references.)
    marketplace_keys = set(cfg.marketplaces)
    for plugin_ref in resolved.claude_plugins:
        bare_name = plugin_ref.split("@")[0]
        if bare_name in cfg.claude_plugins:
            mp_name = cfg.claude_plugins[bare_name].marketplace
            if mp_name not in marketplace_keys:
                failures.append(
                    f"{ctx}: plugin {bare_name!r} references unknown "
                    f"marketplace {mp_name!r}"
                )


@app.command("validate")
def validate(
    profile: str | None = typer.Option(
        None, "--profile", help="Validate a specific profile."
    ),
    all_profiles: bool = typer.Option(
        False, "--all", help="Validate every profile in the YAML."
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Config-shape validation; no filesystem comparison or live target paths."""
    config = _resolve_config_arg(config)
    if profile is not None and all_profiles:
        typer.secho(
            "error: --profile and --all are mutually exclusive",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    if profile is None and not all_profiles:
        typer.secho(
            "error: one of --profile or --all is required",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    from pydantic import ValidationError

    failures: list[str] = []

    # Check 1: Pydantic schema validation + cross-field checks in load_config.
    try:
        cfg = load_config(config)
    except (ValidationError, SetforgeError) as exc:
        typer.echo(f"schema: {exc}")
        raise typer.Exit(1) from exc

    repo_root = config.resolve().parent

    if all_profiles:
        profiles_to_check: list[str] = list(cfg.profiles)
    else:
        assert profile is not None  # guarded above; narrow for mypy
        profiles_to_check = [profile]

    for prof_name in profiles_to_check:
        _check_profile(cfg, prof_name, repo_root, failures)

    if failures:
        for line in failures:
            typer.echo(line)
        raise typer.Exit(1)

    typer.echo("ok")


@app.command()
def fetch() -> None:
    """Clone/fetch the configured git source and check out its pinned ref.

    Resolves the active source via the 4-layer precedence (CLI ``--source``
    > ``SETFORGE_SOURCE`` env > host-local ``local.yaml`` > CWD-fallback).
    For a :class:`setforge.source.PathSource` this is a no-op. For a
    :class:`setforge.source.GitSource`: (1) clone to ``clone_dest`` if
    missing; (2) fetch ``origin``; (3) verify ``tracked/`` is clean
    (refuses to clobber user edits); (4) check out the pinned ``ref``
    (branch or SHA; default ``main``). Auth delegates to the user's
    git/SSH/credential-helper config.
    """
    resolved_source = source_mod.get_resolved_source()
    msg = source_mod.fetch_source(resolved_source)
    typer.echo(msg)


# Subcommand modules — imported for the side effect of @app.command()
# registration. Must run AFTER `app`, the shared option constants, and
# `_resolve_config_arg` are defined above.
from setforge.cli import compare as _compare  # noqa: E402, F401
from setforge.cli import install as _install  # noqa: E402, F401
from setforge.cli import revert as _revert  # noqa: E402, F401
from setforge.cli import sync as _sync  # noqa: E402, F401


def main() -> None:
    """Entry point that wraps ``app`` with :class:`SetforgeError` handling."""
    try:
        app()
    except SetforgeError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        sys.exit(1)


if __name__ == "__main__":
    main()
