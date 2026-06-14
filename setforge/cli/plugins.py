"""plugin + marketplace subcommand groups — manage Claude plugins in setforge.yaml.

``plugin list/add/remove/reconcile/sync-cache`` and ``marketplace
add/remove/update`` both register their sub-apps on the main ``app``
via :func:`typer.Typer.add_typer`.
"""

import subprocess
from pathlib import Path

import typer

from setforge import binaries
from setforge import claude_marketplace_cache as claude_mp_cache_mod
from setforge import claude_plugins as claude_plugins_mod
from setforge import claude_yaml_editor as claude_yaml_editor_mod
from setforge.cli import _CONFIG_OPTION, _PROFILE_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import (
    MARKETPLACE_ADD_EXAMPLES,
    MARKETPLACE_REMOVE_EXAMPLES,
    MARKETPLACE_UPDATE_EXAMPLES,
    PLUGIN_ADD_EXAMPLES,
    PLUGIN_LIST_EXAMPLES,
    PLUGIN_RECONCILE_EXAMPLES,
    PLUGIN_REMOVE_EXAMPLES,
    PLUGIN_SYNC_CACHE_EXAMPLES,
)
from setforge.cli._plugin_helpers import _parse_marketplace_from
from setforge.config import (
    ClaudeInstallMode,
    Config,
    MarketplaceSource,
    ReconcilePolicy,
    load_config,
    resolve_profile,
)
from setforge.errors import MarketplaceCacheMiss, PluginToolMissing

# ---------------------------------------------------------------------------
# plugin sub-app
# ---------------------------------------------------------------------------

plugin_app: typer.Typer = typer.Typer(
    help="Manage Claude plugins in setforge.yaml.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(plugin_app, name="plugin")


@plugin_app.command("list", epilog=PLUGIN_LIST_EXAMPLES)
def plugin_list(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Show declared (YAML) vs installed (claude plugin list) status."""
    config = _resolve_config_arg(config)
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


@plugin_app.command("add", epilog=PLUGIN_ADD_EXAMPLES)
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
    config = _resolve_config_arg(config)
    plugin_name, mp_name = _validate_plugin_add_args(name, marketplace)
    load_config(config)
    source = _parse_marketplace_from(from_)

    _register_plugin_in_yaml(config, profile, plugin_name, mp_name, source)
    if not no_install:
        _execute_plugin_add(plugin_name, mp_name)


def _validate_plugin_add_args(name: str, marketplace: str | None) -> tuple[str, str]:
    """Split ``<name>@<marketplace>`` or pair ``name`` with ``--marketplace``.

    Exits 1 via ``typer.Exit`` when neither form is supplied.
    """
    if "@" in name:
        plugin_name, mp_name = name.split("@", 1)
        return plugin_name, mp_name
    if marketplace:
        return name, marketplace
    typer.secho(
        "error: provide plugin as <name>@<marketplace> or use --marketplace",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


def _resolve_add_source(source: MarketplaceSource, mp_name: str) -> MarketplaceSource:
    """Route a CLI-supplied source through the host-local install-mode policy.

    Under ``claude.install_mode: local-clone`` the reconcile path adds
    marketplaces by their on-disk cache PATH (via
    :func:`resolve_marketplace_source`), and ``_source_identity`` keys
    idempotency on that PATH. The explicit CLI add paths must use the
    identical transform or they (a) hand claude the raw GitHub slug —
    defeating the offline cache — and (b) register a slug-derived source
    the next reconcile cannot match, re-adding the marketplace every run.
    Under ``regular`` mode this is a passthrough.
    """
    host_local = binaries.load_host_local_config()
    return claude_mp_cache_mod.resolve_marketplace_source(
        source,
        host_local.claude.install_mode,
        cache_root=claude_mp_cache_mod.MARKETPLACE_CACHE_ROOT,
        mp_name=mp_name,
        auto=True,
    )


def _register_plugin_in_yaml(
    config: Path,
    profile: str,
    plugin_name: str,
    mp_name: str,
    source: MarketplaceSource,
) -> None:
    """Register the marketplace, plugin, and profile binding in setforge.yaml."""
    mp_added = claude_yaml_editor_mod.yaml_add_marketplace(config, mp_name, source)
    if mp_added:
        typer.echo(f"registered marketplace: {mp_name}")
        try:
            claude_plugins_mod.marketplace_add(
                mp_name, _resolve_add_source(source, mp_name)
            )
            typer.echo(f"marketplace added: {mp_name}")
        except PluginToolMissing as exc:
            typer.secho(f"warning: {exc}", err=True, fg=typer.colors.YELLOW)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            MarketplaceCacheMiss,
        ) as exc:
            # Atomicity: mirror the standalone `marketplace add` path. The YAML
            # entry was written before the binary call, so on failure roll it
            # back or the config repo keeps an orphaned marketplace declaration
            # that diverges from claude's state. Only revert the entry we added.
            claude_yaml_editor_mod.yaml_remove_marketplace(config, mp_name)
            typer.secho(
                f"error: {binaries.stderr_of(exc)}", err=True, fg=typer.colors.RED
            )
            raise typer.Exit(code=1) from exc

    plugin_declared = claude_yaml_editor_mod.yaml_add_plugin(
        config, plugin_name, mp_name
    )
    if plugin_declared:
        typer.echo(f"declared plugin: {plugin_name} @ {mp_name}")

    # Bind the profile to the BARE plugin name: the top-level registry key is
    # the bare name (see yaml_add_plugin above), and every reader of
    # profile.claude_plugins (_validate_plugin_references, _declared_plugin_ids,
    # sync_marketplace_cache) treats entries as bare registry keys. Writing the
    # `@`-form here would brick the config on the next load_config.
    profile_added = claude_yaml_editor_mod.yaml_add_plugin_to_profile(
        config, profile, plugin_name
    )
    if profile_added:
        typer.echo(f"added to {profile}.claude_plugins: {plugin_name}")


def _execute_plugin_add(plugin_name: str, mp_name: str) -> None:
    """Run ``claude plugin install`` then ``claude plugin enable``.

    ``claude plugin install`` writes
    ``installed_plugins.json`` without flipping ``enabledPlugins`` — without
    the second call the plugin lands disabled. Strict failure on enable
    matches the interactive single-plugin shape of ``plugin add``: a silent
    warning would be a footgun. The install half retains today's pattern;
    latent subprocess-error handling on install is tracked separately.
    """
    pid = f"{plugin_name}@{mp_name}"
    try:
        claude_plugins_mod.plugin_install(plugin_name, mp_name)
    except PluginToolMissing as exc:
        typer.secho(
            f"warning: skipping install — {exc}", err=True, fg=typer.colors.YELLOW
        )
        return
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        typer.secho(
            f"ERROR: install failed — {binaries.stderr_of(exc)}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc
    typer.echo(f"installed plugin: {pid}")
    try:
        claude_plugins_mod.plugin_enable(pid)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        typer.secho(
            f"ERROR: enable failed — {binaries.stderr_of(exc)}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc
    typer.echo(f"enabled plugin: {pid}")


def _resolve_disable_id(cfg: Config, name: str) -> str:
    """Build the full ``<name>@<marketplace>`` id that ``claude plugin disable`` needs.

    The ``@``-form is passed straight through. A bare name is resolved
    against the top-level :attr:`Config.claude_plugins` registry to
    recover its marketplace; a bare name absent from the registry exits 1
    with a clear message rather than handing claude an id it rejects.
    """
    if "@" in name:
        return name
    ref = cfg.claude_plugins.get(name)
    if ref is None:
        typer.secho(
            f"error: cannot disable bare plugin {name!r}: not declared in "
            "claude_plugins (pass <name>@<marketplace> explicitly)",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    return f"{name}@{ref.marketplace}"


@plugin_app.command("remove", epilog=PLUGIN_REMOVE_EXAMPLES)
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
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    # Profile bindings are stored under the BARE plugin name (see plugin_add),
    # so strip any trailing @marketplace for the YAML removal to keep it
    # symmetric with the corrected add path.
    bare_ref = name.split("@", 1)[0]
    changed = claude_yaml_editor_mod.yaml_remove_plugin_from_profile(
        config, profile, bare_ref
    )
    if changed:
        typer.echo(f"removed from {profile}.claude_plugins: {bare_ref}")
    else:
        typer.echo(f"not in {profile}.claude_plugins: {bare_ref}")
    if disable:
        # The claude `plugin disable` binary requires the full
        # <name>@<marketplace> id. When the user passed the bare form,
        # reconstruct it from the top-level registry; a pass-through bare id
        # would be rejected by claude and the disable would silently never run.
        disable_id = _resolve_disable_id(cfg, name)
        try:
            claude_plugins_mod.plugin_disable(disable_id)
            typer.echo(f"disabled plugin: {disable_id}")
        except PluginToolMissing as exc:
            typer.secho(f"warning: {exc}", err=True, fg=typer.colors.YELLOW)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            typer.secho(
                f"error: {binaries.stderr_of(exc)}", err=True, fg=typer.colors.RED
            )
            raise typer.Exit(code=1) from exc


@plugin_app.command("reconcile", epilog=PLUGIN_RECONCILE_EXAMPLES)
def plugin_reconcile(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute actions without calling claude CLI."
    ),
) -> None:
    """Explicit reconcile (in addition to the automatic run inside install).

    Exits non-zero when policy is REPORT or --dry-run and there is drift.
    Also exits non-zero when a live run (ADDITIVE/PRUNE) has failed actions
    (``report.failed``).
    """
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    resolved = resolve_profile(cfg, profile)
    try:
        report = claude_plugins_mod.reconcile(cfg, resolved, dry_run=dry_run)
    except PluginToolMissing as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    is_read_only = resolved.plugins_reconcile is ReconcilePolicy.REPORT or dry_run
    _render_reconcile_report(report, is_read_only=is_read_only)
    if not report:
        typer.echo("plugins: nothing to reconcile")
    elif is_read_only:  # noqa: SIM114 — read-only drift and live-run failure are distinct exit conditions; keep branches separate
        raise typer.Exit(code=1)
    elif report.failed:
        raise typer.Exit(code=1)


def _render_reconcile_report(
    report: claude_plugins_mod.ReconcileReport, *, is_read_only: bool
) -> None:
    """Print one line per planned/executed action plus FAILED lines for failures.

    ``is_read_only`` toggles the verb between ``would <action>`` (dry-run /
    ``REPORT`` policy) and the past-tense action (PRUNE/ADDITIVE actually
    ran). Plugin ids that landed in ``report.failed`` are suppressed from
    the action lists so the user doesn't see "installed X" followed by
    "FAILED X" for the same id.
    """
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


@plugin_app.command("sync-cache", epilog=PLUGIN_SYNC_CACHE_EXAMPLES)
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

    config = _resolve_config_arg(config)
    cfg = load_config(config)
    resolved = resolve_profile(cfg, profile)
    try:
        refreshed = claude_mp_cache_mod.sync_marketplace_cache(cfg, resolved)
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

marketplace_app: typer.Typer = typer.Typer(
    help="Manage Claude plugin marketplaces in setforge.yaml.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(marketplace_app, name="marketplace")


@marketplace_app.command("add", epilog=MARKETPLACE_ADD_EXAMPLES)
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
    config = _resolve_config_arg(config)
    source = _parse_marketplace_from(from_)

    try:
        claude_plugins_mod.ensure_claude_available()
    except PluginToolMissing as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    yaml_changed = claude_yaml_editor_mod.yaml_add_marketplace(config, name, source)
    if yaml_changed:
        typer.echo(f"added {name} to marketplaces in YAML")
    else:
        typer.echo(f"marketplace already declared: {name}")

    try:
        claude_plugins_mod.marketplace_add(name, _resolve_add_source(source, name))
        typer.echo(f"registered marketplace: {name}")
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        MarketplaceCacheMiss,
    ) as exc:
        # Atomicity: the YAML entry is written before the binary call. If the
        # binary fails we must roll the entry back, or the config repo is left
        # with an orphaned marketplace declaration that diverges from claude's
        # state. Only revert the entry we just added (yaml_changed).
        if yaml_changed:
            claude_yaml_editor_mod.yaml_remove_marketplace(config, name)
        typer.secho(f"error: {binaries.stderr_of(exc)}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


@marketplace_app.command("remove", epilog=MARKETPLACE_REMOVE_EXAMPLES)
def marketplace_remove_cmd(
    name: str = typer.Argument(..., help="Marketplace name."),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Remove a marketplace from YAML and run claude plugin marketplace remove."""
    config = _resolve_config_arg(config)
    try:
        claude_plugins_mod.ensure_claude_available()
    except PluginToolMissing as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    yaml_changed = claude_yaml_editor_mod.yaml_remove_marketplace(config, name)
    if yaml_changed:
        typer.echo(f"removed {name} from marketplaces in YAML")
    else:
        typer.echo(f"marketplace not found in YAML: {name}")

    try:
        claude_plugins_mod.marketplace_remove(name)
        typer.echo(f"removed marketplace: {name}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        typer.secho(f"error: {binaries.stderr_of(exc)}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


@marketplace_app.command("update", epilog=MARKETPLACE_UPDATE_EXAMPLES)
def marketplace_update_cmd(
    name: str = typer.Argument(..., help="Marketplace name."),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Run claude plugin marketplace update for a named marketplace."""
    # No _resolve_config_arg here: this command never loads the config
    # (it only shells to `claude`), and resolving would add a spurious
    # NoSourceConfigured failure mode for a command that needs no source.
    try:
        claude_plugins_mod.marketplace_update(name)
        typer.echo(f"updated marketplace: {name}")
    except PluginToolMissing as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        typer.secho(f"error: {binaries.stderr_of(exc)}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
