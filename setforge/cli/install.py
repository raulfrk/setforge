"""install subcommand — orchestrates tracked-file deploy + extension/plugin reconcile.

Wires deploy.resolve_deploy / deploy.write_resolved_deploy, extension/plugin
reconcile, and the transition snapshot. Imports ``app`` from
:mod:`setforge.cli` so the ``@app.command()`` registration fires at
module import time; ``setforge/cli/__init__.py`` imports this module at
the bottom for the side effect.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import typer

from setforge import (
    binaries,
    deploy,
    operations,
    reconcile_adapter,
    reconcile_apply,
    transitions,
)
from setforge import claude_plugins as claude_plugins_mod
from setforge import (
    compare as compare_mod,
)
from setforge import secrets as secrets_mod
from setforge import source as source_mod
from setforge import vscode_extensions as vscode_extensions_mod
from setforge._redact import redact_argv
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli import _install_helpers as install_helpers_mod
from setforge.cli._git_check import (
    resolve_source_for_git_check,
    run_git_check_or_raise,
)
from setforge.cli._help_examples import INSTALL_EXAMPLES
from setforge.cli._helpers import (
    ProfileContext,
    _iter_all_tracked_files,
    _parse_section_auto,
)
from setforge.cli._install_helpers import (
    _dry_run_pipeline,
    _install_recorded_nothing,
    _PendingDeploy,
    _run_predeploy_gates,
    _want_interactive_reconcile,
    _write_install_transition,
)
from setforge.cli._lock_enumerate import enumerate_lock_items
from setforge.cli._mcp_helpers import (
    MCPInstallPlan,
    plan_mcp_servers,
    reconcile_mcp_servers,
)
from setforge.cli._plugin_helpers import (
    _emit_reconcile_summary,
    _reconcile_extensions,
    _reconcile_plugins,
)
from setforge.cli._provision_helpers import reconcile_packages
from setforge.cli._secrets_confirm import prompt_secret_action
from setforge.cli._welcome import (
    WelcomeChoice,
    build_welcome_inventory,
    is_fresh_host,
    prompt_welcome,
    reject_auto_on_fresh_host,
)
from setforge.config import (
    Config,
    LocalOverlayResolution,
    ResolvedProfile,
    TrackedFile,
    load_config,
    refuse_unmigrated_host_local_leak,
    resolve_effective_profile,
)
from setforge.errors import ExtensionToolMissing, PluginToolMissing, SetforgeError
from setforge.lockfile import LockFile, lock_path, parse_lock
from setforge.locking import mutation_locks
from setforge.provision.dispatch import (
    ProvisioningPlan,
    has_hard_failure,
    plan_provisioning,
    validate_provisioning,
)
from setforge.provision.lock_apply import extension_pins, plugin_pins
from setforge.provision.protocol import Outcome, ReconcileResult
from setforge.reconcile import host_local_record
from setforge.secrets import SecretAction, SecretsScanResult
from setforge.transitions import (
    ReconcileStatus,
    load_latest,
    load_reconcile_outcomes,
)


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """Read-only install decisions consumed by preview and apply."""

    ctx: ProfileContext
    host_local_sections: Mapping[
        str, Mapping[source_mod.HostLocalSectionName, source_mod.HostLocalSection]
    ]
    drift_report: compare_mod.CompareReport
    deploys: tuple[_PendingDeploy, ...]
    bootstrap: tuple[Path, ...]
    dst_paths: tuple[Path, ...]
    source_bytes: tuple[tuple[Path, bytes | None], ...]
    tracked_entries: tuple[tuple[TrackedFile, str, Path, Path], ...]
    live_paths: tuple[tuple[Path, _LivePathFingerprint], ...]
    file_pre: Mapping[Path, str | None]
    provisioning: ProvisioningPlan
    mcp: MCPInstallPlan
    extensions: vscode_extensions_mod.ExtensionPlan | None
    plugins: claude_plugins_mod.PluginPlan | None


def _provisioning_plan_has_work(plan: ProvisioningPlan) -> bool:
    """Return whether applying the frozen package plan may change the host."""
    return bool(plan.bundles) or any(
        not batch.delta.is_empty() for batch in plan.batches
    )


@dataclass(frozen=True, slots=True)
class _LivePathFingerprint:
    """Identity, link topology, bytes, and mode for one planned live path."""

    kind: int | None
    mode: int | None
    link_target: str | None
    effective_kind: int | None
    effective_mode: int | None
    effective_bytes: bytes | None


@dataclass(frozen=True, slots=True)
class SecretPlan:
    """Approved allowlist writes deferred until the install apply phase."""

    hashes: tuple[str, ...]
    allowlist_path: Path


def _load_validated_host_local_sections(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    profile: str,
) -> dict[str, dict[source_mod.HostLocalSectionName, source_mod.HostLocalSection]]:
    """Compatibility seam delegating to the shared overlay loader."""
    return install_helpers_mod._load_validated_host_local_sections(
        cfg, resolved, repo_root, profile
    )


def _freeze_host_local_sections(
    sections: dict[
        str, dict[source_mod.HostLocalSectionName, source_mod.HostLocalSection]
    ],
) -> Mapping[
    str, Mapping[source_mod.HostLocalSectionName, source_mod.HostLocalSection]
]:
    return MappingProxyType(
        {name: MappingProxyType(dict(values)) for name, values in sections.items()}
    )


def _snapshot_inputs(paths: set[Path]) -> tuple[tuple[Path, bytes | None], ...]:
    """Capture file bytes/absence in deterministic path order."""
    return tuple(
        (path, path.read_bytes() if path.is_file() else None) for path in sorted(paths)
    )


def _snapshot_live_paths(
    paths: set[Path],
) -> tuple[tuple[Path, _LivePathFingerprint], ...]:
    """Capture path identity plus followed target state without mutation."""
    snapshots: list[tuple[Path, _LivePathFingerprint]] = []
    for path in sorted(paths):
        try:
            own = path.lstat()
        except FileNotFoundError:
            snapshots.append(
                (
                    path,
                    _LivePathFingerprint(None, None, None, None, None, None),
                )
            )
            continue
        own_kind = stat.S_IFMT(own.st_mode)
        link_target = None
        if stat.S_ISLNK(own.st_mode):
            try:
                link_target = str(path.readlink())
            except OSError as exc:
                raise SetforgeError(
                    f"live install path changed while snapshotting {path}; retry"
                ) from exc
        try:
            effective = path.stat()
            effective_kind = stat.S_IFMT(effective.st_mode)
            effective_mode = stat.S_IMODE(effective.st_mode)
        except FileNotFoundError:
            effective_kind = None
            effective_mode = None
            effective_bytes = None
        except OSError as exc:
            raise SetforgeError(
                f"live install path changed while snapshotting {path}; retry"
            ) from exc
        else:
            try:
                effective_bytes = (
                    path.read_bytes() if stat.S_ISREG(effective.st_mode) else None
                )
            except OSError as exc:
                raise SetforgeError(
                    f"live install path changed while snapshotting {path}; retry"
                ) from exc
        snapshots.append(
            (
                path,
                _LivePathFingerprint(
                    kind=own_kind,
                    mode=stat.S_IMODE(own.st_mode),
                    link_target=link_target,
                    effective_kind=effective_kind,
                    effective_mode=effective_mode,
                    effective_bytes=effective_bytes,
                ),
            )
        )
    return tuple(snapshots)


def _load_install_context(
    config: Path, profile: str, repo_root: Path, *, locked: bool
) -> tuple[
    ProfileContext,
    LockFile | None,
    LocalOverlayResolution,
    tuple[tuple[Path, bytes | None], ...],
]:
    """Load config/overlay/lock from one stable byte snapshot."""
    input_paths = {config.resolve(), binaries.LOCAL_CONFIG_PATH, lock_path(config)}
    baseline = _snapshot_inputs(input_paths)
    cfg = load_config(config)
    refuse_unmigrated_host_local_leak(cfg, verb="install", profile=profile)
    effective = resolve_effective_profile(cfg, profile, repo_root)
    active_lock = _prepare_lock(config, cfg, effective.resolved, locked=locked)
    if _snapshot_inputs(input_paths) != baseline:
        raise SetforgeError("install configuration changed while loading; retry")
    return (
        ProfileContext(
            cfg=cfg,
            resolved=effective.resolved,
            repo_root=repo_root,
            profile=profile,
        ),
        active_lock,
        effective.local_overlay,
        baseline,
    )


def _build_install_plan(
    ctx: ProfileContext,
    *,
    section_auto: reconcile_apply.ReconcileAuto | None,
    interactive: bool,
    lock: LockFile | None,
    transition: bool,
    input_baseline: tuple[tuple[Path, bytes | None], ...],
    auto: bool,
) -> InstallPlan:
    """Compute every tracked-file decision before the first install write."""
    tracked_entries = tuple(_iter_all_tracked_files(ctx))
    source_paths = {path for path, _payload in input_baseline}
    source_paths.update(sub_src for _, _, sub_src, _ in tracked_entries)
    source_bytes = _snapshot_inputs(source_paths)
    source_map = dict(source_bytes)
    if any(source_map.get(path) != payload for path, payload in input_baseline):
        raise SetforgeError("install configuration changed before planning; retry")
    dst_paths = tuple(
        [
            Path(tf.symlink).expanduser() if tf.symlink is not None else sub_dst
            for tf, _, _, sub_dst in tracked_entries
        ]
        + [Path(str(path)).expanduser() for path in ctx.resolved.bootstrap]
    )
    live_paths = {
        path
        for tf, _, _, sub_dst in tracked_entries
        for path in (
            sub_dst,
            Path(tf.symlink).expanduser() if tf.symlink is not None else sub_dst,
        )
    }
    live_paths.update(Path(str(path)).expanduser() for path in ctx.resolved.bootstrap)
    live_path_snapshot = _snapshot_live_paths(live_paths)
    file_pre = MappingProxyType(transitions.snapshot_paths(dst_paths))
    host_local = _load_validated_host_local_sections(
        ctx.cfg, ctx.resolved, ctx.repo_root, ctx.profile
    )
    frozen_host_local = _freeze_host_local_sections(host_local)
    deploy.validate_srcs_exist(ctx.cfg, ctx.resolved, ctx.repo_root)
    if transition:
        transitions.validate_state_dir_writable()
    drift_report = compare_mod.compare_profile(
        ctx.cfg,
        ctx.profile,
        ctx.repo_root,
        host_local_sections=host_local,
    )
    deploys = install_helpers_mod._plan_tracked_files(
        ctx,
        host_local_sections_map=frozen_host_local,
        section_auto=section_auto,
        interactive=interactive,
    )
    extensions: vscode_extensions_mod.ExtensionPlan | None = None
    extension_input = reconcile_adapter.extensions_input(ctx.cfg, ctx.resolved)
    if extension_input.include or extension_input.exclude:
        try:
            extensions = vscode_extensions_mod.plan_reconcile(
                extension_input, pins=extension_pins(lock)
            )
        except ExtensionToolMissing as exc:
            typer.secho(
                f"warning: skipping extension reconcile — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
    plugins: claude_plugins_mod.PluginPlan | None = None
    if reconcile_adapter.plugin_bare_names(ctx.cfg, ctx.resolved):
        try:
            plugins = claude_plugins_mod.plan_reconcile(
                ctx.cfg,
                declared_plugin_ids=reconcile_adapter.plugin_ids(ctx.cfg, ctx.resolved),
                policy=reconcile_adapter.plugin_policy(ctx.resolved),
                pins=plugin_pins(lock),
                auto=auto,
            )
        except PluginToolMissing as exc:
            typer.secho(
                f"warning: skipping Claude plugin reconcile — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
    provisioning = plan_provisioning(ctx.cfg, ctx.resolved, lock=lock)
    mcp = plan_mcp_servers(ctx.cfg, ctx.resolved)
    planned_entries = tuple(
        (record.tracked_file, record.sub_name, record.sub_src, record.sub_dst)
        for record in deploys
    )
    expected_names = tuple(sub_name for _, sub_name, _, _ in tracked_entries)
    compared_names = tuple(entry.name for entry in drift_report.entries)
    if (
        planned_entries != tracked_entries
        or tuple(_iter_all_tracked_files(ctx)) != tracked_entries
        or compared_names != expected_names
    ):
        raise SetforgeError("tracked file inventory changed during planning; retry")
    if _snapshot_inputs(source_paths) != source_bytes:
        raise SetforgeError("install inputs changed during planning; retry")
    _assert_live_paths_unchanged(live_path_snapshot)
    if transitions.snapshot_paths(dst_paths) != dict(file_pre):
        raise SetforgeError("live install targets changed during planning; retry")
    return InstallPlan(
        ctx=ctx,
        host_local_sections=frozen_host_local,
        drift_report=drift_report,
        deploys=deploys,
        bootstrap=tuple(
            Path(str(path)).expanduser() for path in ctx.resolved.bootstrap
        ),
        dst_paths=dst_paths,
        source_bytes=source_bytes,
        tracked_entries=tracked_entries,
        live_paths=live_path_snapshot,
        file_pre=file_pre,
        provisioning=provisioning,
        mcp=mcp,
        extensions=extensions,
        plugins=plugins,
    )


def _assert_plan_inputs_unchanged(plan: InstallPlan) -> None:
    """Refuse if source or live inputs changed before the first write."""
    if tuple(_iter_all_tracked_files(plan.ctx)) != plan.tracked_entries:
        raise SetforgeError("tracked file inventory changed after planning; retry")
    changed = [
        path
        for path, payload in plan.source_bytes
        if (path.read_bytes() if path.is_file() else None) != payload
    ]
    if changed:
        names = ", ".join(str(path) for path in changed)
        raise SetforgeError(f"install inputs changed after planning: {names}; retry")
    _assert_live_paths_unchanged(plan.live_paths)
    if transitions.snapshot_paths(plan.dst_paths) != dict(plan.file_pre):
        raise SetforgeError("live install targets changed after planning; retry")


def _assert_live_paths_unchanged(
    expected: tuple[tuple[Path, _LivePathFingerprint], ...],
) -> None:
    """Refuse link retargets, type swaps, content edits, and mode changes."""
    if _snapshot_live_paths({path for path, _ in expected}) != expected:
        raise SetforgeError(
            "live install targets changed after planning: path topology changed; retry"
        )


def _validate_external_plan(plan: InstallPlan) -> None:
    """Recheck adapter preconditions without changing the selected operations."""
    validate_provisioning(plan.provisioning)
    if plan.extensions is not None:
        vscode_extensions_mod.validate_plan(plan.extensions)
    if plan.plugins is not None:
        claude_plugins_mod.validate_plan(plan.plugins)
    if plan.mcp.value is not None:
        from setforge import mcp_servers

        mcp_servers.validate_plan(plan.mcp.value)


def _apply_extension_plan(
    plan: InstallPlan,
    *,
    retry_failed_ids: frozenset[str],
    yes: bool,
    lock: LockFile | None,
) -> tuple[
    transitions.ExtensionDelta | None,
    tuple[transitions.ReconcileOutcome, ...],
]:
    if plan.extensions is None:
        return None, ()
    return _reconcile_extensions(
        plan.ctx.cfg,
        plan.ctx.resolved,
        retry_failed_ids=retry_failed_ids,
        yes=yes,
        pins=extension_pins(lock),
        plan=plan.extensions,
    )


def _apply_plugin_plan(
    plan: InstallPlan,
    *,
    retry_failed_ids: frozenset[str],
    yes: bool,
    lock: LockFile | None,
) -> tuple[
    transitions.PluginDelta | None,
    tuple[transitions.ReconcileOutcome, ...],
]:
    if plan.plugins is None:
        return None, ()
    return _reconcile_plugins(
        plan.ctx.cfg,
        plan.ctx.resolved,
        retry_failed_ids=retry_failed_ids,
        yes=yes,
        pins=plugin_pins(lock),
        plan=plan.plugins,
    )


def _render_install_plan(plan: InstallPlan, scan_result: SecretsScanResult) -> None:
    """Render the same immutable plan the real install path consumes."""
    _dry_run_pipeline(
        ctx=plan.ctx,
        drift_report=plan.drift_report,
        deploys=plan.deploys,
        provisioning=plan.provisioning,
        mcp=plan.mcp,
        extensions=plan.extensions,
        plugins=plan.plugins,
        immutable_plan=True,
        secrets_scan=scan_result,
        host_local_sections_map=plan.host_local_sections,
    )


def _fetch_upstream(
    install_source: source_mod.Source, *, no_fetch: bool, dry_run: bool
) -> None:
    """Fetch the git config source before deploy (the A0 fetch-upstream step).

    A :class:`~setforge.source.PathSource` no-ops inside ``fetch_source``;
    ``--no-fetch`` skips the pull entirely for offline / CI runs (a missing
    GitSource clone then surfaces a clean ``SourceNotCloned`` downstream
    rather than a silent network touch). On ``--dry-run`` the pull is only
    announced (WOULD-prefixed), never performed. A ``GitOpError`` /
    ``DirtySourceCheckout`` propagates as a ``SetforgeError`` and aborts the
    install before any tracked file is written. Only a real GitSource pull
    echoes a status line, so a PathSource install stays quiet.
    """
    if no_fetch:
        return
    is_git = isinstance(install_source, source_mod.GitSource)
    if dry_run:
        if is_git:
            typer.echo("WOULD fetch upstream config source")
        return
    fetch_message = source_mod.fetch_source(install_source)
    if is_git:
        typer.echo(fetch_message)


def _prepare_lock(
    config: Path, cfg: Config, resolved: ResolvedProfile, *, locked: bool
) -> LockFile | None:
    """Load the committed lock and, under ``--locked``, gate on its coverage.

    The coverage check runs FIRST (before any mutation), so a missing lockable
    entry aborts here.
    """
    path = lock_path(config)
    active_lock = (
        parse_lock(path.read_text(encoding="utf-8")) if path.exists() else None
    )
    if locked:
        _gate_on_lock_coverage(cfg, resolved, active_lock)
    return active_lock


def _gate_on_lock_coverage(
    cfg: Config, resolved: ResolvedProfile, lock: LockFile | None
) -> None:
    """Fail-closed unless every LOCKABLE package has a lock entry.

    ``--locked`` is a spec→lock COVERAGE check, NOT a re-resolve, scoped to
    exactly :func:`~setforge.cli._lock_enumerate.enumerate_lock_items` — NOT
    the full plan, so ``cargo_binaries``/bundle-inline packages never false-fail.
    """
    present = (
        {(pin.type.value, pin.key) for pin in lock.packages}
        if lock is not None
        else set()
    )
    missing = [
        item
        for item in enumerate_lock_items(cfg, resolved)
        if (item.pkg_type.value, item.lock_key()) not in present
    ]
    if not missing:
        return
    names = ", ".join(f"{item.lock_key()} ({item.pkg_type.value})" for item in missing)
    typer.secho(
        f"error: --locked but these packages have no setforge.lock entry: "
        f"{names} — run `setforge lock --profile=<name>`",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


@app.command(epilog=INSTALL_EXAMPLES)
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
        help=(
            "Resolve permission-mode drift non-interactively by reapplying "
            "the tracked mode."
        ),
    ),
    auto_accept_live: bool = typer.Option(
        False,
        "--auto-accept-live",
        help=(
            "Proceed past permission-mode drift non-interactively; install "
            "still reapplies the tracked mode (live permission bits are not kept)."
        ),
    ),
    reconcile_user_sections: bool = typer.Option(
        False,
        "--reconcile-user-sections",
        help=(
            "Interactively reconcile drifted `shared` user-sections. "
            "Mutually exclusive with --auto."
        ),
    ),
    auto: str | None = typer.Option(
        None,
        "--auto",
        help=(
            "Non-interactive section reconciliation: 'use-tracked' "
            "deploys tracked-side updates into every shared section; "
            "'keep-live' silences shared-drift warnings and keeps live. "
            "Mutually exclusive with --reconcile-user-sections."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the --auto* confirmation prompt (for non-interactive use).",
    ),
    no_secrets_scan: bool = typer.Option(
        False,
        "--no-secrets-scan",
        help="Skip pre-deploy secrets scan (gitleaks) for automation.",
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help=(
            "Re-attempt only the items skipped during the previous install's "
            "reconcile (per the prior transition's reconcile_outcomes). "
            "Other reconcile work is suppressed for this run."
        ),
    ),
    no_git_check: bool = typer.Option(
        False,
        "--no-git-check",
        help=(
            "Skip the pre-deploy git-status check on the config source. "
            "Intended for CI / cron — bypasses the dirty-tree / "
            "cache-lag warning on path / git sources respectively."
        ),
    ),
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help=(
            "Skip the pre-deploy upstream fetch of a git config source "
            "(offline / air-gapped / CI). The install reconciles against the "
            "already-checked-out clone; nothing is pulled. A path source "
            "never fetches, so this flag is a no-op there."
        ),
    ),
    locked: bool = typer.Option(
        False,
        "--locked",
        help=(
            "Fail (non-zero) unless every lockable package in the resolved "
            "profile has a matching setforge.lock entry (spec→lock coverage). "
            "Does NOT re-resolve; the install still consumes the lock offline."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Simulate every install phase without mutating the filesystem, "
            "transition state, or extension/plugin reconcilers. Output is "
            "WOULD-prefixed for mutating verbs; the final line is "
            "'=== rerun without --dry-run to apply for real ==='."
        ),
    ),
) -> None:
    """Deploy tracked → live for every tracked_file in the profile."""
    # Canonicalize once so a symlink retarget cannot split source discovery,
    # locking, config loading, and input snapshots across two repositories.
    config = _resolve_config_arg(config).resolve()
    # Mutual-exclusivity guard for the legacy unexpected-drift flags.
    if auto_accept_tracked and auto_accept_live:
        typer.secho(
            "error: --auto-accept-tracked and --auto-accept-live are"
            " mutually exclusive",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    # Mutual-exclusivity guard for the new section-reconcile flags.
    section_auto = _parse_section_auto(auto, reconcile_user_sections)

    repo_root = config.parent
    # Source acquisition precedes config loading so this invocation plans the
    # checkout it just fetched, rather than a stale in-memory model.
    # Dry-run builds the same plan as apply and renders it without entering the
    # mutation phase. The flag stays at this orchestration boundary.
    if dry_run:
        install_source = resolve_source_for_git_check(repo_root)
        _fetch_upstream(install_source, no_fetch=no_fetch, dry_run=True)
        run_git_check_or_raise(source=install_source, no_git_check=no_git_check)
        ctx, active_lock, _local_overlay, input_baseline = _load_install_context(
            config, profile, repo_root, locked=locked
        )
        plan = _build_install_plan(
            ctx,
            section_auto=section_auto,
            interactive=False,
            lock=active_lock,
            transition=not no_transition,
            input_baseline=input_baseline,
            auto=True,
        )
        scan_result = secrets_mod.run_pre_deploy_scan(
            tracked_root=config.parent / "tracked",
            skip=no_secrets_scan,
        )
        _render_install_plan(plan, scan_result)
        return

    with (
        mutation_locks(
            resources=True,
            config_dir=config.parent,
            profile=profile,
        ),
        operations.recover_on_error(profile, "install"),
    ):
        operations.refuse_active(profile)
        install_source = resolve_source_for_git_check(repo_root)
        _fetch_upstream(install_source, no_fetch=no_fetch, dry_run=False)
        run_git_check_or_raise(source=install_source, no_git_check=no_git_check)
        ctx, active_lock, local_overlay, input_baseline = _load_install_context(
            config, profile, repo_root, locked=locked
        )
        cfg = ctx.cfg
        resolved = ctx.resolved
        fresh = is_fresh_host()
        interactive = _want_interactive_reconcile(
            reconcile_user_sections=reconcile_user_sections,
            section_auto=section_auto,
        )
        plan = _build_install_plan(
            ctx,
            section_auto=section_auto,
            interactive=interactive,
            lock=active_lock,
            transition=not no_transition,
            input_baseline=input_baseline,
            auto=yes,
        )
        scan_result = secrets_mod.run_pre_deploy_scan(
            tracked_root=config.parent / "tracked",
            skip=no_secrets_scan,
        )
        if fresh:
            reject_auto_on_fresh_host(auto=auto)
            inventory = build_welcome_inventory(ctx, local_overlay=local_overlay)
            welcome_choice = prompt_welcome(
                inventory=inventory,
                yes=yes,
                run_dry_run=lambda: _render_install_plan(plan, scan_result),
            )
            if welcome_choice is not WelcomeChoice.PROCEED:
                return

        _run_predeploy_gates(
            drift_report=plan.drift_report,
            ctx=ctx,
            auto_accept_tracked=auto_accept_tracked,
            auto_accept_live=auto_accept_live,
            yes=yes,
        )

        # Refuse-before-write: pre-flight the symlink-dst clobber refusal.
        # deploy_symlinked_file() raises when a regular file or directory
        # already sits at a symlink tracked_file's dst, but that check only
        # fires at pass-2 WRITE time — a symlink ordered after regular files
        # would let those earlier writes land, then abort with no transition
        # recorded (un-revertable partial install). Surfacing the same
        # condition HERE, before any mutation (seed commit, overlay migration,
        # or deploy), keeps the install all-or-nothing.
        _refuse_on_symlink_dst_conflicts(ctx)

        secret_plan = _plan_secret_findings(scan_result, yes=yes)
        if secret_plan is None:
            typer.secho(
                "install aborted by secrets scan", err=True, fg=typer.colors.RED
            )
            raise typer.Exit(code=1)

        # This is the first mutation boundary. Every refusal and confirmation
        # above has completed, and apply consumes the frozen plan below.
        _assert_plan_inputs_unchanged(plan)
        _validate_external_plan(plan)
        if not no_transition:
            transitions.ensure_state_dir_writable()

        state_pre = install_helpers_mod._capture_store_snapshots(profile, plan.deploys)
        secrets_checkpoint_paths = (
            *plan.bootstrap,
            *((secret_plan.allowlist_path,) if secret_plan.hashes else ()),
        )
        journal_paths = tuple(
            dict.fromkeys(
                (
                    *plan.dst_paths,
                    *(sub_dst for _, _, _, sub_dst in plan.tracked_entries),
                    *secrets_checkpoint_paths,
                )
            )
        )
        tracked_checkpoint_paths = tuple(
            dict.fromkeys(
                (
                    *plan.dst_paths,
                    *(sub_dst for _, _, _, sub_dst in plan.tracked_entries),
                )
            )
        )
        adapter_snapshots = _install_adapter_snapshots(plan)
        adapter_kinds = {item.kind for item in adapter_snapshots}
        journal = operations.prepare(
            command="install",
            profile=profile,
            config_dir=config.parent,
            resources_lock=True,
            command_line=tuple(redact_argv(sys.argv[1:])),
            paths=journal_paths,
            state_snapshots=state_pre,
            adapters=adapter_snapshots,
        )
        journal = _apply_secrets_and_bootstrap(
            journal,
            secret_plan=secret_plan,
            bootstrap=plan.bootstrap,
            checkpoint_paths=secrets_checkpoint_paths,
        )

        # SOFT failures warn but never gate; HARD gates after the transition.
        if _provisioning_plan_has_work(plan.provisioning):
            journal = operations.begin_checkpoint(
                journal,
                name="packages",
                kind=operations.CheckpointKind.IRREVERSIBLE,
                recovery=(
                    "inspect package-manager output and receipts; SetForge will not "
                    "guess an uninstall for potentially user-owned software"
                ),
                paths=(),
                restore_state=False,
                restore_transitions=False,
                adapters=(),
            )
            provision_results = reconcile_packages(
                cfg, resolved, lock=active_lock, plan=plan.provisioning
            )
            journal = operations.finish_checkpoint(journal)
        else:
            provision_results = reconcile_packages(
                cfg, resolved, lock=active_lock, plan=plan.provisioning
            )

        # For symlink-deployed tracked_files the recorded "touched path" is
        # the symlink's TARGET (where bytes actually land), not the link
        # path itself: GNU patch refuses to patch a symlink as a regular
        # file, so a transition recording the link path would brick revert.
        dst_paths = list(plan.dst_paths)
        # Store files (byte bases, spans sidecars, scalar-base manifests) do
        # NOT ride this patch snapshot: their pre-install state is captured
        # at the pass-2 barrier (state_snapshots below) and revert restores
        # them through that mechanism — recording them here too would
        # double-restore (Invariant I5 now lives in the snapshot path).

        file_pre = dict(plan.file_pre)

        # Interactive reconcile: resolve conflicts through the reconcile
        # engine's per-region wizard ONLY when this install is in
        # interactive-reconcile mode AND stdout is a tty (the same gate the
        # shared user-section wizard uses). Non-tty / --auto ⇒ False, so the
        # driver keeps the bare warn-and-defer / auto behavior.
        journal = operations.begin_checkpoint(
            journal,
            name="tracked-files-and-stores",
            kind=operations.CheckpointKind.REVERSIBLE,
            recovery="restore captured paths and reconcile-store snapshots",
            paths=tracked_checkpoint_paths,
            restore_state=True,
            restore_transitions=False,
            adapters=(),
        )
        deploy_outcome = install_helpers_mod._apply_tracked_file_plan(
            profile, plan.deploys
        )

        # Seed AFTER deploy so its pre-install snapshot is the revert baseline.
        seeded = host_local_record.seed_section_slots_to_store(
            cfg, resolved, repo_root, profile
        )
        if seeded:
            typer.secho(
                f"seeded host-local section template(s): {', '.join(sorted(seeded))}",
                err=True,
                fg=typer.colors.GREEN,
            )
        journal = operations.finish_checkpoint(journal)

        retry_failed_ids = (
            _collect_retry_failed_ids(profile) if retry_failed else frozenset()
        )
        journal = operations.begin_checkpoint(
            journal,
            name="extensions",
            kind=operations.CheckpointKind.COMPENSATABLE,
            recovery="restore the frozen pre-install extension inventory",
            paths=(),
            restore_state=False,
            restore_transitions=False,
            adapters=(operations.AdapterKind.EXTENSIONS,)
            if operations.AdapterKind.EXTENSIONS in adapter_kinds
            else (),
        )
        ext_delta, ext_outcomes = _apply_extension_plan(
            plan,
            retry_failed_ids=retry_failed_ids,
            yes=yes,
            lock=active_lock,
        )
        journal = operations.finish_checkpoint(journal)
        journal = operations.begin_checkpoint(
            journal,
            name="plugins-and-marketplaces",
            kind=operations.CheckpointKind.COMPENSATABLE,
            recovery="restore frozen plugin and marketplace inventories",
            paths=(),
            restore_state=False,
            restore_transitions=False,
            adapters=(operations.AdapterKind.PLUGINS,)
            if operations.AdapterKind.PLUGINS in adapter_kinds
            else (),
        )
        plugin_delta, plugin_outcomes = _apply_plugin_plan(
            plan,
            retry_failed_ids=retry_failed_ids,
            yes=yes,
            lock=active_lock,
        )
        journal = operations.finish_checkpoint(journal)
        journal = operations.begin_checkpoint(
            journal,
            name="mcp-servers",
            kind=operations.CheckpointKind.COMPENSATABLE,
            recovery="restore frozen MCP registrations",
            paths=(),
            restore_state=False,
            restore_transitions=False,
            adapters=(operations.AdapterKind.MCP,)
            if operations.AdapterKind.MCP in adapter_kinds
            else (),
        )
        mcp_delta, mcp_failed = reconcile_mcp_servers(cfg, resolved, plan=plan.mcp)
        journal = operations.finish_checkpoint(journal)

        file_post = transitions.snapshot_paths(dst_paths)

        _emit_reconcile_summary(plugin_outcomes, ext_outcomes)

        if not no_transition and not _install_recorded_nothing(
            file_pre=file_pre,
            file_post=file_post,
            deploy_outcome=deploy_outcome,
            ext_delta=ext_delta,
            plugin_delta=plugin_delta,
            mcp_delta=mcp_delta,
            reconcile_outcomes=plugin_outcomes + ext_outcomes,
            seeded=bool(seeded),
        ):
            journal = operations.begin_checkpoint(
                journal,
                name="transition-record",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery="remove the transition record committed by this install",
                paths=(),
                restore_state=False,
                restore_transitions=True,
                adapters=(),
            )
            target = _write_install_transition(
                profile,
                file_pre,
                file_post,
                ext_delta,
                plugin_delta,
                source_dir=ctx.repo_root,
                reconcile_outcomes=plugin_outcomes + ext_outcomes,
                state_snapshots=deploy_outcome.state_snapshots,
                mcp_delta=mcp_delta,
                file_modes=deploy_outcome.prior_modes,
            )
            typer.echo(f"transition: {target}")
            typer.echo(f"↩  revert with: setforge revert --profile={profile}")
            journal = operations.finish_checkpoint(journal)

        operations.complete(journal)

        _gate_on_mcp_failures(mcp_failed)
        _gate_on_provisioning_failures(provision_results)
        _gate_on_deferred_reconcile(deploy_outcome.deferred_reconcile, interactive)


def _gate_on_deferred_reconcile(
    deferred: tuple[Path, ...],
    interactive: bool,
) -> None:
    """Exit non-zero when a non-interactive install left reconcile conflicts.

    A plain file whose conflict DEFERRED non-interactively (no TTY, no
    ``--auto``) keeps live but leaves the upstream change unresolved. The
    transition is already written (the partial install stays revertable); this
    gate signals the unresolved set so CI / cron fails loudly instead of
    silently passing over a conflict. An interactive run (``interactive`` True)
    already let the user choose Skip per region, so it does NOT gate — those
    defers warned per file during the deploy.
    """
    if not deferred or interactive:
        return
    count = len(deferred)
    typer.secho(
        f"error: {count} file{'s' if count != 1 else ''} deferred with "
        "unresolved conflicts (see the per-file warnings above) — re-run "
        "`setforge install` interactively, or pass --auto=keep-live / "
        "--auto=use-tracked to resolve non-interactively.",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


def _install_adapter_snapshots(
    plan: InstallPlan,
) -> tuple[operations.AdapterSnapshot, ...]:
    """Project frozen install-plan inventories into recovery baselines."""
    snapshots: list[operations.AdapterSnapshot] = []
    if plan.extensions is not None:
        snapshots.append(
            operations.AdapterSnapshot(
                operations.AdapterKind.EXTENSIONS,
                json.dumps(sorted(plan.extensions.installed)),
            )
        )
    if plan.plugins is not None:
        snapshots.append(
            operations.AdapterSnapshot(
                operations.AdapterKind.PLUGINS,
                json.dumps(
                    {
                        "plugins": plan.plugins.pre_plugins,
                        "marketplaces": plan.plugins.pre_marketplaces,
                    },
                    sort_keys=True,
                ),
            )
        )
    if plan.mcp.value is not None:
        snapshots.append(
            operations.AdapterSnapshot(
                operations.AdapterKind.MCP,
                json.dumps(
                    [
                        {
                            "name": name,
                            "prior": (
                                None if prior is None else [list(prior[0]), prior[1]]
                            ),
                        }
                        for name, prior in plan.mcp.value.preconditions
                    ],
                    sort_keys=True,
                ),
            )
        )
    return tuple(snapshots)


def _refuse_on_symlink_dst_conflicts(ctx: ProfileContext) -> None:
    """Refuse the install when a symlink tracked_file's dst is already occupied.

    Mirrors the refusal in :func:`deploy.deploy_symlinked_file` (a regular file
    or a directory — but NOT a pre-existing symlink — sitting at the link's
    ``dst``), but runs as a pass-1 refuse-before-write gate so the abort fires
    BEFORE any file is written or any store / local.yaml is mutated. Without
    this pre-flight the same condition only surfaces at pass-2 write time, where
    a symlink ordered after regular-file tracked_files would let those earlier
    writes land and then raise with no transition recorded — an un-revertable
    partial install. Every conflicting dst is collected so the user sees the
    complete set in one aggregated error rather than one failure per attempt.
    """
    failures: list[str] = []
    for tracked_file, _sub_name, _sub_src, sub_dst in _iter_all_tracked_files(ctx):
        if tracked_file.symlink is None:
            continue
        if sub_dst.is_symlink() or not sub_dst.exists():
            continue
        kind = "directory" if sub_dst.is_dir() else "regular file"
        failures.append(
            f"refusing to deploy symlink at {sub_dst}: a {kind} is already "
            f"present. Move it aside or remove it before deploying "
            f"tracked_file with symlink: {tracked_file.symlink!r}."
        )
    if failures:
        raise SetforgeError("\n".join(failures))


def _gate_on_mcp_failures(mcp_failed: list[tuple[str, str]]) -> None:
    """Exit non-zero when any declared MCP server failed to register.

    Cargo failures do NOT gate (a crate that won't build is a soft,
    host-specific outcome — the warning already surfaced), but a declared
    MCP server that could not be registered is a hard reconcile failure.
    """
    if not mcp_failed:
        return
    names = ", ".join(name for name, _err in mcp_failed)
    typer.secho(
        f"install completed with MCP server failures: {names}",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


def _gate_on_provisioning_failures(results: list[ReconcileResult]) -> None:
    if not has_hard_failure(results):
        return
    names = ", ".join(
        outcome.item.identity.display
        for result in results
        for outcome in result.outcomes
        if outcome.outcome is Outcome.HARD
    )
    typer.secho(
        f"install completed with package-provisioning failures: {names}",
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


def _plan_secret_findings(
    scan_result: SecretsScanResult,
    *,
    yes: bool,
    allowlist_path: Path | None = None,
) -> SecretPlan | None:
    """Collect secret decisions without writing the allowlist."""
    target = allowlist_path or (
        Path.home() / ".config" / "setforge" / "secrets-allowlist"
    )
    seen: set[str] = set()
    approved: list[str] = []
    for finding in scan_result.findings:
        if finding.snippet_hash in seen:
            continue
        seen.add(finding.snippet_hash)
        action = prompt_secret_action(finding, yes=yes)
        if action is SecretAction.ABORT:
            return None
        if action is SecretAction.ALLOWLIST:
            approved.append(finding.snippet_hash)
    return SecretPlan(hashes=tuple(approved), allowlist_path=target)


def _apply_secret_plan(plan: SecretPlan) -> None:
    """Persist the allowlist choices already approved during planning."""
    for snippet_hash in plan.hashes:
        secrets_mod.append_to_allowlist(
            snippet_hash=snippet_hash,
            allowlist_path=plan.allowlist_path,
        )


def _apply_secrets_and_bootstrap(
    journal: operations.OperationJournal,
    *,
    secret_plan: SecretPlan,
    bootstrap: tuple[Path, ...],
    checkpoint_paths: tuple[Path, ...],
) -> operations.OperationJournal:
    """Apply the first reversible phase without claiming untouched paths."""
    if not checkpoint_paths:
        _apply_secret_plan(secret_plan)
        deploy.bootstrap_local(bootstrap)
        return journal
    applying = operations.begin_checkpoint(
        journal,
        name="secrets-and-bootstrap",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore captured allowlist and bootstrap paths",
        paths=checkpoint_paths,
        restore_state=False,
        restore_transitions=False,
        adapters=(),
    )
    _apply_secret_plan(secret_plan)
    deploy.bootstrap_local(bootstrap)
    return operations.finish_checkpoint(applying)


def _collect_retry_failed_ids(profile: str) -> frozenset[str]:
    """Read the previous transition's ``reconcile_outcomes`` and return
    the set of items whose status was ``"skipped"``.

    Returns an empty :class:`frozenset` when there's no prior transition
    or the previous transition has no ``reconcile_outcomes.json`` file
    (backward-compat path for transitions written before the schema bump).
    Used by ``setforge install --retry-failed`` to filter the reconcile
    work list to only those previously-failed ids.
    """
    prev = load_latest(profile)
    if prev is None:
        return frozenset()
    outcomes = load_reconcile_outcomes(prev)
    return frozenset(o.item_id for o in outcomes if o.status is ReconcileStatus.SKIPPED)
