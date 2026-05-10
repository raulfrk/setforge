"""Typer CLI entry point for ``my-setup``.

Commands wired in Pillar 1: ``install``, ``compare``, ``capture``, ``sync``.
Pillar 2 adds extension reconcile inside ``install``. Claude plugin
reconcile lands in Pillar 3. ``revert`` (dotfiles-19n) replays the most
recent transition for a profile in reverse.
"""

import json
import logging
import subprocess
import sys
from datetime import UTC
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax

from my_setup import binaries, deploy, transitions, vscode_extensions
from my_setup import capture as capture_mod
from my_setup import claude_plugins as claude_plugins_mod
from my_setup import compare as compare_mod
from my_setup import merge as merge_mod
from my_setup.compare import CompareStatus, expand_dotfile, resolve_dst, resolve_src
from my_setup.config import Config, ReconcilePolicy, load_config, resolve_profile
from my_setup.errors import (
    CaptureRequiresInteractive,
    ExtensionInstallFailed,
    ExtensionToolMissing,
    MySetupError,
    NoTransitionFound,
    PluginToolMissing,
)

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


@app.callback()
def _root(
    code_bin: str | None = typer.Option(
        None,
        "--code-bin",
        help="Override path to the 'code' (VSCode) binary. "
        "Takes precedence over MY_SETUP_CODE_BIN and ~/.config/my-setup/local.yaml.",
    ),
    claude_bin: str | None = typer.Option(
        None,
        "--claude-bin",
        help="Override path to the 'claude' binary. "
        "Takes precedence over MY_SETUP_CLAUDE_BIN and ~/.config/my-setup/local.yaml.",
    ),
    patch_bin: str | None = typer.Option(
        None,
        "--patch-bin",
        help="Override path to the GNU 'patch' binary. "
        "Takes precedence over MY_SETUP_PATCH_BIN and ~/.config/my-setup/local.yaml.",
    ),
) -> None:
    """Wire host-local binary overrides and ensure the local config stub exists."""
    binaries.set_cli_overrides(code=code_bin, claude=claude_bin, patch=patch_bin)
    binaries.ensure_local_config_stub()


@app.command()
def install(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    no_transition: bool = typer.Option(
        False,
        "--no-transition",
        hidden=True,
        help="Skip writing a transition record (testing / debugging).",
    ),
    auto_accept_tracked: bool = typer.Option(
        False,
        "--auto-accept-tracked",
        help="Non-interactively resolve unexpected drift by keeping tracked values.",
    ),
    auto_accept_live: bool = typer.Option(
        False,
        "--auto-accept-live",
        help="Non-interactively resolve unexpected drift by adopting live values.",
    ),
) -> None:
    """Deploy tracked → live for every dotfile in the profile."""
    # Mutual-exclusivity guard
    if auto_accept_tracked and auto_accept_live:
        typer.secho(
            "error: --auto-accept-tracked and --auto-accept-live are mutually exclusive",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)

    if not no_transition:
        transitions.ensure_state_dir_writable()
    deploy.validate_srcs_exist(cfg, resolved, repo_root)
    deploy.bootstrap_local(resolved.bootstrap)

    # P4.3: check for unexpected drift before deploying.
    # Only DRIFTED entries (existing live files that diverge from tracked
    # in unexpected ways) gate install. MISSING entries are expected on
    # first install and are handled by deploy below.
    drift_report = compare_mod.compare_profile(cfg, profile, repo_root)
    has_real_unexpected = any(
        e.status == CompareStatus.DRIFTED and e.unexpected_drift_keys
        for e in drift_report.entries
    )
    if has_real_unexpected:
        unexpected_count = sum(
            1
            for e in drift_report.entries
            if e.status == CompareStatus.DRIFTED and e.unexpected_drift_keys
        )
        if auto_accept_tracked:
            # Non-interactively resolve as [k] — deploy will overwrite live
            merge_mod.run_wizard(
                drift_report,
                cfg,
                repo_root,
                my_setup_yaml_path=config.resolve(),
                profile=profile,
                auto_accept="k",
            )
        elif auto_accept_live:
            # Non-interactively resolve as [u] — update tracked to match live
            merge_mod.run_wizard(
                drift_report,
                cfg,
                repo_root,
                my_setup_yaml_path=config.resolve(),
                profile=profile,
                auto_accept="u",
            )
        else:
            typer.secho(
                f"unexpected drift in {unexpected_count} file(s): "
                f"run 'my-setup merge --profile={profile}' to resolve, "
                f"or pass --auto-accept-tracked or --auto-accept-live",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)

    dst_paths: list[Path] = []
    for name in resolved.dotfiles:
        dotfile = cfg.dotfiles[name]
        src = resolve_src(dotfile, repo_root)
        dst = resolve_dst(dotfile)
        for _, _, sub_dst in expand_dotfile(name, src, dst):
            dst_paths.append(sub_dst)
    dst_paths.extend(Path(str(p)).expanduser() for p in resolved.bootstrap)

    file_pre = transitions.snapshot_paths(dst_paths)

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
                preserve_user_keys_deep=dotfile.preserve_user_keys_deep or None,
            )
            typer.echo(f"{result.action.value:>8}  {sub_dst}")

    ext_delta: transitions.ExtensionDelta | None = None
    try:
        report = vscode_extensions.reconcile(resolved.extensions)
    except ExtensionToolMissing as exc:
        typer.secho(
            f"warning: skipping extension reconcile — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
    else:
        failed_ids = {ext_id for ext_id, _ in report.failed}
        for ext_id in report.to_install:
            if ext_id not in failed_ids:
                typer.echo(f"installed  {ext_id}")
        for ext_id in report.to_uninstall:
            if ext_id not in failed_ids:
                typer.echo(f"uninstalled  {ext_id}")
        for ext_id, err in report.failed:
            typer.secho(f"FAILED  {ext_id} — {err}", err=True, fg=typer.colors.YELLOW)
        if not report:
            typer.echo("extensions: nothing to reconcile")
        ext_delta = transitions.ExtensionDelta(
            added=[i for i in report.to_install if i not in failed_ids],
            removed=[i for i in report.to_uninstall if i not in failed_ids],
        )

    # Step 5: Claude plugin reconcile (warn-and-skip if claude absent).
    try:
        plugin_report = claude_plugins_mod.reconcile(cfg, resolved)
    except PluginToolMissing as exc:
        typer.secho(
            f"warning: skipping claude plugin reconcile — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
    else:
        failed_plugin_ids = {pid for pid, _ in plugin_report.failed}
        for name, mp in plugin_report.to_install:
            pid = f"{name}@{mp}"
            if pid not in failed_plugin_ids:
                typer.echo(f"plugin installed  {pid}")
        for pid in plugin_report.to_enable:
            if pid not in failed_plugin_ids:
                typer.echo(f"plugin enabled    {pid}")
        for pid in plugin_report.to_disable:
            if pid not in failed_plugin_ids:
                typer.echo(f"plugin disabled   {pid}")
        for pid, err in plugin_report.failed:
            typer.secho(
                f"FAILED plugin  {pid} — {err}", err=True, fg=typer.colors.YELLOW
            )
        if not plugin_report:
            typer.echo("plugins: nothing to reconcile")

    file_post = transitions.snapshot_paths(dst_paths)

    if not no_transition:
        target = transitions.write_transition(
            transitions.make_meta(transitions.TransitionCommand.INSTALL, profile),
            file_pre,
            file_post,
            ext_delta,
        )
        typer.echo(f"transition: {target}")


@app.command()
def compare(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    full_diff: bool = typer.Option(
        False,
        "--full-diff",
        "--full",
        help="Append unified diff body below the summary table.",
    ),
    check: bool = typer.Option(
        False, "--check", help="Exit non-zero on unexpected drift (for CI)."
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="With --check: exit 1 on any drift (expected or unexpected).",
    ),
) -> None:
    """Report drift between tracked and live for every dotfile in the profile."""
    cfg = load_config(config)
    repo_root = config.resolve().parent
    report = compare_mod.compare_profile(cfg, profile, repo_root)

    console = Console()
    table = compare_mod.compare_summary_table(report)
    console.print(table)

    # Counts below the table
    unchanged_count = sum(
        1 for e in report.entries if e.status == CompareStatus.UNCHANGED
    )
    missing_count = sum(1 for e in report.entries if e.status == CompareStatus.MISSING)
    if unchanged_count:
        console.print(f"UNCHANGED: {unchanged_count} files")
    if missing_count:
        console.print(f"MISSING: {missing_count} files")

    if full_diff:
        for entry in report.entries:
            if entry.diff:
                console.print(Syntax(entry.diff, "diff"))

    if check:
        if strict:
            if any(e.status == CompareStatus.DRIFTED for e in report.entries):
                raise typer.Exit(code=1)
        elif report.has_unexpected_drift:
            raise typer.Exit(code=1)


@app.command()
def capture(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    auto: str | None = typer.Option(
        None,
        "--auto",
        help=(
            "Non-interactive resolution for capture-time drift: "
            "'use-live' absorbs all drift (today's silent-absorb "
            "behavior), 'keep-tracked' rejects all drift."
        ),
    ),
) -> None:
    """Capture live → tracked for every dotfile in the profile.

    When tracked declares ``preserve_user_keys_deep`` or carries
    non-preserve top-level drift, the merge wizard fires interactively;
    pass ``--auto={use-live, keep-tracked}`` for non-interactive
    contexts.
    """
    if auto is not None and auto not in {"use-live", "keep-tracked"}:
        typer.secho(
            f"error: --auto must be 'use-live' or 'keep-tracked' (got {auto!r})",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    cfg = load_config(config)
    repo_root = config.resolve().parent
    try:
        results = capture_mod.capture_profile(
            cfg,
            profile,
            repo_root,
            my_setup_yaml_path=config.resolve(),
            auto=auto,  # type: ignore[arg-type]
        )
    except CaptureRequiresInteractive as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        typer.secho(
            "capture cancelled (Ctrl-C); files restored from snapshot",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(130)
    for result in results:
        typer.echo(f"{result.action.value:>8}  {result.name}")


@app.command()
def merge(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    dotfile: str | None = typer.Option(
        None,
        "--dotfile",
        help="Narrow the walk to one dotfile entry key.",
    ),
) -> None:
    """Interactively resolve unexpected drift for every dotfile in the profile."""
    cfg = load_config(config)
    repo_root = config.resolve().parent
    report = compare_mod.compare_profile(cfg, profile, repo_root)

    if not report.has_unexpected_drift:
        typer.echo("no unexpected drift; nothing to do.")
        raise typer.Exit(0)

    try:
        merge_mod.run_wizard(
            report,
            cfg,
            repo_root,
            my_setup_yaml_path=config.resolve(),
            profile=profile,
            dotfile_filter=dotfile,
        )
    except KeyboardInterrupt:
        typer.secho(
            "merge cancelled (Ctrl-C); files restored from snapshot",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(130)


@app.command()
def sync(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    no_transition: bool = typer.Option(
        False,
        "--no-transition",
        hidden=True,
        help="Skip writing a transition record (testing / debugging).",
    ),
    auto: str | None = typer.Option(
        None,
        "--auto",
        help=(
            "Non-interactive resolution for capture-time drift: "
            "'use-live' absorbs all drift (today's silent-absorb "
            "behavior), 'keep-tracked' rejects all drift. Without TTY "
            "and without --auto, sync exits 1 with "
            "CaptureRequiresInteractive."
        ),
    ),
) -> None:
    """Capture live → tracked for dotfiles and extensions.

    When tracked declares ``preserve_user_keys_deep`` or carries
    non-preserve top-level drift, the merge wizard fires interactively
    so you can review each diverged sub-key / top-level key; this
    behavior is symmetric with ``my-setup install``'s drift gate. Pass
    ``--auto=use-live`` to reproduce the pre-`nen.23` silent-absorb
    behavior (e.g. for scripted runs) or ``--auto=keep-tracked`` to
    refuse to absorb drift.
    """
    if auto is not None and auto not in {"use-live", "keep-tracked"}:
        typer.secho(
            f"error: --auto must be 'use-live' or 'keep-tracked' (got {auto!r})",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)

    if not no_transition:
        transitions.ensure_state_dir_writable()

    src_paths: list[Path] = []
    for name in resolved.dotfiles:
        dotfile = cfg.dotfiles[name]
        src = resolve_src(dotfile, repo_root)
        dst = resolve_dst(dotfile)
        for _, sub_src, _ in expand_dotfile(name, src, dst):
            src_paths.append(sub_src)
    src_paths.append(config.resolve())

    file_pre = transitions.snapshot_paths(src_paths)

    try:
        results = capture_mod.capture_profile(
            cfg,
            profile,
            repo_root,
            my_setup_yaml_path=config.resolve(),
            auto=auto,  # type: ignore[arg-type]
        )
    except CaptureRequiresInteractive as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        typer.secho(
            "sync cancelled (Ctrl-C); files restored from snapshot",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(130)
    for result in results:
        typer.echo(f"{result.action.value:>8}  {result.name}")

    try:
        changed = vscode_extensions.capture_extensions(config, profile)
    except ExtensionToolMissing as exc:
        typer.secho(
            f"warning: skipping extension capture — {exc}",
            err=True,
            fg=typer.colors.YELLOW,
        )
    else:
        typer.echo(f"extensions: include {'updated' if changed else 'unchanged'}")

    file_post = transitions.snapshot_paths(src_paths)

    if not no_transition:
        target = transitions.write_transition(
            transitions.make_meta(transitions.TransitionCommand.SYNC, profile),
            file_pre,
            file_post,
            None,  # sync's extension change is reflected in the YAML diff
        )
        typer.echo(f"transition: {target}")


def _reverse_extensions(
    delta: dict,
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Apply the inverse of an extensions.json delta.

    Returns ``(reverse_added, reverse_removed, failed)``. Per-extension
    failures are caught (warn-and-continue) so the reverse transition
    still gets written; ``failed`` records ``(ext_id, error_msg)`` for
    logging by the caller.
    """
    reverse_added: list[str] = []
    reverse_removed: list[str] = []
    failed: list[tuple[str, str]] = []
    for ext_id in delta.get("added", []):
        try:
            vscode_extensions.uninstall_one(ext_id)
            reverse_removed.append(ext_id)
        except ExtensionToolMissing as exc:
            typer.secho(
                f"warning: skipping uninstall of {ext_id} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
        except ExtensionInstallFailed as exc:
            failed.append((ext_id, str(exc)))
            typer.secho(
                f"FAILED  uninstall {ext_id} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
    for ext_id in delta.get("removed", []):
        try:
            vscode_extensions.install_one(ext_id)
            reverse_added.append(ext_id)
        except ExtensionToolMissing as exc:
            typer.secho(
                f"warning: skipping install of {ext_id} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
        except ExtensionInstallFailed as exc:
            failed.append((ext_id, str(exc)))
            typer.secho(
                f"FAILED  install {ext_id} — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
    return reverse_added, reverse_removed, failed


@app.command()
def revert(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Revert the most recent transition for the named profile.

    Applies the recorded patch in reverse and reverses any extension
    delta (uninstalling what was installed, re-installing what was
    uninstalled). Records its own reverse transition so a second
    revert invocation acts as redo.
    """
    transition = transitions.load_latest(profile)
    if transition is None:
        raise NoTransitionFound(f"no transition history for profile {profile!r}")

    transitions.ensure_state_dir_writable()
    typer.echo(f"reverting: {transition}")

    meta_payload = json.loads((transition / "meta.json").read_text(encoding="utf-8"))
    touched_paths = [Path(p) for p in meta_payload.get("paths", [])]
    file_pre = transitions.snapshot_paths(touched_paths)

    transitions.apply_patch_reverse(transition)

    ext_file = transition / "extensions.json"
    reverse_added: list[str] = []
    reverse_removed: list[str] = []
    if ext_file.exists():
        delta = json.loads(ext_file.read_text())
        reverse_added, reverse_removed, _ = _reverse_extensions(delta)

    file_post = transitions.snapshot_paths(touched_paths)
    reverse_meta = transitions.make_meta(transitions.TransitionCommand.REVERT, profile)
    reverse_delta: transitions.ExtensionDelta | None = None
    if reverse_added or reverse_removed:
        reverse_delta = transitions.ExtensionDelta(
            added=reverse_added, removed=reverse_removed
        )
    target = transitions.write_transition(
        reverse_meta, file_pre, file_post, reverse_delta
    )
    typer.echo(f"transition: {target}")


transitions_app = typer.Typer(
    help="Inspect transition history for install/sync/revert.",
    no_args_is_help=True,
)
app.add_typer(transitions_app, name="transitions")


@transitions_app.command("list")
def transitions_list(
    profile: list[str] = typer.Option(
        [],
        "--profile",
        "-p",
        help="Filter to specified profile(s). Repeatable; OR-filter.",
    ),
    reverse: bool = typer.Option(
        False, "--reverse", help="Newest-first instead of oldest-first."
    ),
) -> None:
    """List recorded transitions across all profiles."""
    listings = transitions.list_transitions(
        profile_filter=list(profile) if profile else None,
        reverse=reverse,
    )
    if not listings:
        typer.echo("(no transitions)")
        return
    rows = [
        (
            entry.timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            entry.command,
            entry.profile,
            str(entry.file_count),
            str(entry.ext_count),
            entry.directory.name,
        )
        for entry in listings
    ]
    headers = ("TIMESTAMP", "COMMAND", "PROFILE", "FILES", "EXTS", "DIRECTORY")
    widths = [
        max(len(headers[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(len(headers))
    ]
    typer.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=False)))
    for row in rows:
        typer.echo("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=False)))


@transitions_app.command("show")
def transitions_show(
    prefix: str = typer.Argument(..., help="Dirname or unique-prefix match."),
) -> None:
    """Show metadata and per-file action summary for one transition."""
    target = transitions.resolve_transition_prefix(prefix)
    meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
    typer.echo(f"DIRECTORY  {target.name}")
    typer.echo(f"COMMAND    {meta.get('command', '')}")
    typer.echo(f"PROFILE    {meta.get('profile', '')}")
    typer.echo(f"TIMESTAMP  {meta.get('timestamp', '')}")
    if "host" in meta:
        typer.echo(f"HOST       {meta['host']}")
    if "version" in meta:
        typer.echo(f"VERSION    {meta['version']}")

    file_actions = transitions.summarize_transition(target)
    if file_actions:
        typer.echo("")
        typer.echo("FILES")
        action_width = max(len(action) for action in file_actions.values())
        for path, action in sorted(file_actions.items()):
            typer.echo(f"  {action.ljust(action_width)}  {path}")

    ext_file = target / "extensions.json"
    if ext_file.exists():
        ext_payload = json.loads(ext_file.read_text(encoding="utf-8"))
        added = ext_payload.get("added", []) or []
        removed = ext_payload.get("removed", []) or []
        if added or removed:
            typer.echo("")
            typer.echo("EXTENSIONS")
            for ext_id in added:
                typer.echo(f"  added    {ext_id}")
            for ext_id in removed:
                typer.echo(f"  removed  {ext_id}")


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
    help="Manage Claude plugins in my_setup.yaml.",
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
        help="Plugin name (in <name>@<marketplace> form or just <name> with --marketplace).",
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
    from my_setup.config import MarketplaceSource, MarketplaceSourceKind

    if from_.startswith("github:"):
        repo = from_[len("github:") :]
        source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo=repo)
    elif from_.startswith("path:"):
        local_path = Path(from_[len("path:") :]).expanduser()
        source = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=local_path)
    else:
        typer.secho(
            f"error: unrecognised --from format {from_!r}; use github:owner/repo or path:/dir",
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
    # dotfiles-l37). Strict failure on enable matches the interactive
    # single-plugin shape of `plugin add`: a silent warning would be a
    # footgun. The install half retains today's pattern; latent
    # subprocess-error handling on install is tracked separately as
    # dotfiles-oyv.
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


# ---------------------------------------------------------------------------
# marketplace sub-app
# ---------------------------------------------------------------------------

marketplace_app = typer.Typer(
    help="Manage Claude plugin marketplaces in my_setup.yaml.",
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
    from my_setup.config import MarketplaceSource, MarketplaceSourceKind

    if from_.startswith("github:"):
        repo = from_[len("github:") :]
        source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo=repo)
    elif from_.startswith("path:"):
        local_path = Path(from_[len("path:") :]).expanduser()
        source = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=local_path)
    else:
        typer.secho(
            f"error: unrecognised --from format {from_!r}; use github:owner/repo or path:/dir",
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
    """Run checks 2–6 for a single profile, appending failures in-place."""
    from jinja2 import StrictUndefined, Template, TemplateSyntaxError, UndefinedError

    from my_setup.compare import resolve_src
    from my_setup.paths import template_context

    ctx = f"profile {prof_name!r}"

    # Check 2: Profile resolution (covers missing profiles + cycle detection).
    try:
        resolved = resolve_profile(cfg, prof_name)
    except MySetupError as exc:
        failures.append(f"{ctx}: {exc}")
        return

    for dotfile_name in resolved.dotfiles:
        dotfile = cfg.dotfiles[dotfile_name]
        dot_ctx = f"{ctx}: dotfile {dotfile_name!r}"

        # Check 3: Jinja2 dst template renderability (StrictUndefined catches typos).
        if dotfile.template:
            try:
                Template(dotfile.dst, undefined=StrictUndefined).render(
                    **template_context()
                )
            except (TemplateSyntaxError, UndefinedError) as exc:
                failures.append(f"{dot_ctx}: unrenderable dst template: {exc}")
                continue

        # Check 4: tracked src exists on disk.
        src = resolve_src(dotfile, repo_root)
        if not src.exists():
            failures.append(f"{dot_ctx}: src {dotfile.src} does not exist")

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
    """Config-shape validation. No filesystem comparison; no live target paths needed."""
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
    except (ValidationError, MySetupError) as exc:
        typer.echo(f"schema: {exc}")
        raise typer.Exit(1) from exc

    repo_root = config.resolve().parent

    profiles_to_check: list[str] = (
        list(cfg.profiles) if all_profiles else [profile]  # type: ignore[list-item]
    )

    for prof_name in profiles_to_check:
        _check_profile(cfg, prof_name, repo_root, failures)

    if failures:
        for line in failures:
            typer.echo(line)
        raise typer.Exit(1)

    typer.echo("ok")


def main() -> None:
    """Entry point that wraps ``app`` with :class:`MySetupError` handling."""
    try:
        app()
    except MySetupError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        sys.exit(1)


if __name__ == "__main__":
    main()
