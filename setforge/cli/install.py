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
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import typer

from setforge import (
    atomicio,
    binaries,
    deploy,
    operations,
    reconcile_adapter,
    reconcile_apply,
    transitions,
)
from setforge import claude_plugins as claude_plugins_mod
from setforge import codex_plugins as codex_plugins_mod
from setforge import codex_resources as codex_resources_mod
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
    _iter_all_trees,
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
    TreeSymlinkPolicy,
    load_config,
    refuse_unmigrated_host_local_leak,
    resolve_effective_profile,
)
from setforge.errors import ExtensionToolMissing, PluginToolMissing, SetforgeError
from setforge.file_ownership import (
    FileAction,
    FileDecision,
    decide_file,
    observe_file,
    observe_tree,
    publish_file_claim_locked,
)
from setforge.generated import resolve_generated
from setforge.lockfile import LockFile, lock_path, parse_lock
from setforge.locking import MutationLockGuards, mutation_locks
from setforge.ownership import (
    OwnershipError,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    load_or_create_owner_id,
    read_owner_id,
)
from setforge.provision.bundle import resolve_bundle_items
from setforge.provision.capability_graph import (
    CapabilityActivation,
    CapabilityGraph,
    CapabilityNode,
    CapabilityStatus,
    CapabilityTargetAction,
    CapabilityTargetKind,
)
from setforge.provision.dispatch import (
    ProvisioningPlan,
    has_hard_failure,
    plan_provisioning,
    publish_installed_package_claims_locked,
    resolve_provision_items,
    validate_provisioning,
)
from setforge.provision.lock_apply import extension_pins, plugin_pins
from setforge.provision.ownership import (
    PackageAction,
    PackageDecision,
    publish_claim_locked,
)
from setforge.provision.protocol import ObservationOrigin, Outcome, ReconcileResult
from setforge.provision.receipt import ReceiptStore, default_receipt_root
from setforge.reconcile import host_local_record
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.types import file_id
from setforge.secrets import SecretAction, SecretsScanResult
from setforge.transitions import (
    ReconcileStatus,
    load_latest,
    load_reconcile_outcomes,
)
from setforge.tree_management import (
    TreePlan,
    apply_tree,
    inventory_path,
    plan_tree,
    read_inventory,
    scan_tree,
    temporary_entry_name,
    write_inventory,
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
    ownership_pre: Mapping[Path, str | None]
    file_ownership: tuple[FileDecision, ...]
    trees: tuple[PlannedTree, ...]
    provisioning: ProvisioningPlan
    package_owner_id: UUID | None
    mcp: MCPInstallPlan
    extensions: vscode_extensions_mod.ExtensionPlan | None
    plugins: claude_plugins_mod.PluginPlan | None
    codex_plugins: codex_plugins_mod.CodexPluginPlan | None
    codex_configs: tuple[codex_resources_mod.CodexConfigPlan, ...]
    codex_trusted_projects: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannedTree:
    """One frozen explicit tree plan and its root authority decision."""

    tracked_file: TrackedFile
    name: str
    source: Path
    destination: Path
    plan: TreePlan
    decision: FileDecision


@dataclass(frozen=True, slots=True)
class _CapabilityApplyResult:
    """Outputs of the four application capability target phases."""

    journal: operations.OperationJournal
    provision_results: tuple[ReconcileResult, ...]
    deploy_outcome: install_helpers_mod.DeployOutcome
    seeded: tuple[str, ...]
    ext_delta: transitions.ExtensionDelta | None
    ext_outcomes: tuple[transitions.ReconcileOutcome, ...]
    plugin_delta: transitions.PluginDelta | None
    plugin_outcomes: tuple[transitions.ReconcileOutcome, ...]
    codex_plugin_delta: transitions.CodexPluginDelta | None
    codex_plugin_failed: tuple[tuple[str, str], ...]


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


def _build_install_plan(  # noqa: C901 - freezes every install input in one pass
    ctx: ProfileContext,
    *,
    section_auto: reconcile_apply.ReconcileAuto | None,
    interactive: bool,
    lock: LockFile | None,
    transition: bool,
    input_baseline: tuple[tuple[Path, bytes | None], ...],
    auto: bool,
    package_owner_id: UUID | None = None,
) -> InstallPlan:
    """Compute every tracked-file decision before the first install write."""
    tracked_entries = tuple(_iter_all_tracked_files(ctx))
    tree_entries = tuple(_iter_all_trees(ctx))
    codex_configs = codex_resources_mod.plan_config_resources(
        ctx.cfg,
        ctx.resolved,
        ctx.repo_root,
        read_base=lambda resource_id: reconcile_store.read_base(
            ctx.profile, file_id(resource_id)
        ),
        stored_ids=tuple(map(str, reconcile_store.stored_file_ids(ctx.profile))),
    )
    codex_trusted_projects = codex_resources_mod.selected_trusted_projects(
        ctx.cfg,
        ctx.resolved,
        ctx.repo_root,
        stored_ids=tuple(map(str, reconcile_store.stored_file_ids(ctx.profile))),
    )
    source_paths = {path for path, _payload in input_baseline}
    source_paths.update(sub_src for _, _, sub_src, _ in tracked_entries)
    source_paths.update(source for _, _, source, _ in tree_entries)
    source_paths.update(
        source for codex_plan in codex_configs for source in codex_plan.sources
    )
    source_bytes = _snapshot_inputs(source_paths)
    source_map = dict(source_bytes)
    if any(source_map.get(path) != payload for path, payload in input_baseline):
        raise SetforgeError("install configuration changed before planning; retry")
    trees = _plan_trees(tree_entries, profile=ctx.profile, owner_id=package_owner_id)
    blocked_trees = tuple(tree.name for tree in trees if tree.plan.blocked)
    if blocked_trees:
        raise SetforgeError(
            "managed tree conflicts require review: " + ", ".join(blocked_trees)
        )
    dst_paths = tuple(
        [
            Path(tf.symlink).expanduser() if tf.symlink is not None else sub_dst
            for tf, _, _, sub_dst in tracked_entries
        ]
        + [Path(str(path)).expanduser() for path in ctx.resolved.bootstrap]
        + [codex_plan.destination for codex_plan in codex_configs]
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
    live_paths.update(codex_plan.destination for codex_plan in codex_configs)
    live_path_snapshot = _snapshot_live_paths(live_paths)
    file_pre = MappingProxyType(transitions.snapshot_paths(dst_paths))
    file_ownership = _plan_file_ownership(
        tracked_entries, profile=ctx.profile, owner_id=package_owner_id
    ) + tuple(tree.decision for tree in trees)
    ownership_pre = MappingProxyType(
        transitions.snapshot_paths(
            tuple(
                OwnershipStore().claim_path(decision.observation.resource_id)
                for decision in file_ownership
            )
        )
    )
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
        ownership_authorized={
            **_file_ownership_authorization(tracked_entries, file_ownership),
            **{
                tree.name: tree.decision.action
                not in {FileAction.ADOPT, FileAction.HOLD}
                for tree in trees
            },
        },
        resolved=ctx.resolved,
    )
    deploys = install_helpers_mod._plan_tracked_files(
        ctx,
        host_local_sections_map=frozen_host_local,
        section_auto=section_auto,
        interactive=interactive,
    )
    deploys = _hold_generated_adoptions(deploys, file_ownership)
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
    codex_plugins: codex_plugins_mod.CodexPluginPlan | None = None
    codex_plugin_ids = reconcile_adapter.codex_plugin_ids(ctx.cfg, ctx.resolved)
    if codex_plugin_ids and ctx.cfg.codex is not None:
        try:
            codex_plugins = codex_plugins_mod.plan_reconcile(
                declared_plugin_ids=codex_plugin_ids,
                marketplaces=ctx.cfg.codex.marketplaces,
                policy=reconcile_adapter.codex_plugin_policy(ctx.resolved),
            )
        except PluginToolMissing as exc:
            typer.secho(
                f"warning: skipping Codex plugin reconcile — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
    provisioning = _plan_owned_provisioning(ctx, lock=lock, owner_id=package_owner_id)
    mcp = plan_mcp_servers(ctx.cfg, ctx.resolved)
    planned_entries = tuple(
        (record.tracked_file, record.sub_name, record.sub_src, record.sub_dst)
        for record in deploys
    )
    expected_names = tuple(sub_name for _, sub_name, _, _ in tracked_entries) + tuple(
        tree.name for tree in trees
    )
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
    if transitions.snapshot_paths(ownership_pre) != dict(ownership_pre):
        raise SetforgeError("file ownership changed during planning; retry")
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
        ownership_pre=ownership_pre,
        file_ownership=file_ownership,
        trees=trees,
        provisioning=provisioning,
        package_owner_id=package_owner_id,
        mcp=mcp,
        extensions=extensions,
        plugins=plugins,
        codex_plugins=codex_plugins,
        codex_configs=codex_configs,
        codex_trusted_projects=codex_trusted_projects,
    )


def _hold_generated_adoptions(
    deploys: tuple[_PendingDeploy, ...],
    decisions: tuple[FileDecision, ...],
) -> tuple[_PendingDeploy, ...]:
    """Keep present external generated bytes unchanged during metadata adoption."""
    adopting = {
        decision.observation.locator
        for decision in decisions
        if decision.action is FileAction.ADOPT
    }
    held: list[_PendingDeploy] = []
    for record in deploys:
        if record.generated is None:
            held.append(record)
            continue
        content_path = (
            Path(record.tracked_file.symlink).expanduser()
            if record.tracked_file.symlink is not None
            else record.sub_dst
        )
        if str(content_path.absolute()) not in adopting or not content_path.is_file():
            held.append(record)
            continue
        live = content_path.read_text(encoding="utf-8")
        if record.resolved is not None:
            held.append(
                replace(
                    record,
                    resolved=replace(record.resolved, content=live),
                    preview_action=deploy.DeployAction.NOOP,
                )
            )
        else:
            held.append(replace(record, symlink_content=live))
    return tuple(held)


def _plan_owned_provisioning(
    ctx: ProfileContext, *, lock: LockFile | None, owner_id: UUID | None
) -> ProvisioningPlan:
    return plan_provisioning(
        ctx.cfg,
        ctx.resolved,
        lock=lock,
        ownership_store=OwnershipStore(),
        owner_id=owner_id,
    )


def _plan_file_ownership(
    tracked_entries: tuple[tuple[TrackedFile, str, Path, Path], ...],
    *,
    profile: str,
    owner_id: UUID | None,
) -> tuple[FileDecision, ...]:
    """Freeze container authority independently from unit classifications."""
    store = OwnershipStore()
    planning_owner = owner_id or UUID(int=0)
    return tuple(
        decide_file(
            observation,
            store.read(observation.resource_id),
            owner_id=planning_owner,
            protected_units=_has_protected_units(profile, name),
        )
        for tracked, name, _src, destination in tracked_entries
        for owned_destination in _ownership_destinations(tracked, destination)
        for observation in (
            observe_file(
                owned_destination,
                allow_topology=(
                    tracked.symlink is not None and owned_destination == destination
                ),
            ),
        )
    )


def _plan_trees(
    tree_entries: tuple[tuple[TrackedFile, str, Path, Path], ...],
    *,
    profile: str,
    owner_id: UUID | None,
) -> tuple[PlannedTree, ...]:
    """Freeze desired/live/prior inventories and root authority decisions."""
    store = OwnershipStore()
    planning_owner = owner_id or UUID(int=0)
    planned: list[PlannedTree] = []
    for tracked, name, source, destination in tree_entries:
        policy = tracked.tree
        if policy is None:  # pragma: no cover - iterator contract
            raise SetforgeError(f"managed tree {name!r} has no tree policy")
        desired = scan_tree(source, policy, capture_payloads=True)
        if not desired.inventory.root_present:
            raise SetforgeError(f"managed tree source is missing: {source}")
        live_policy = policy.model_copy(update={"symlinks": TreeSymlinkPolicy.PRESERVE})
        live = scan_tree(destination, live_policy).inventory
        prior = read_inventory(profile, name)
        tree_plan = plan_tree(desired, live, prior, policy)
        observation = observe_tree(destination, live.fingerprint)
        decision = decide_file(
            observation,
            store.read(observation.resource_id),
            owner_id=planning_owner,
        )
        if (
            owner_id is None
            and prior is not None
            and decision.action is FileAction.ADOPT
        ):
            decision = FileDecision(
                observation,
                None,
                (
                    FileAction.MANAGE
                    if live.fingerprint == prior.fingerprint
                    else FileAction.REVIEW
                ),
                "legacy non-Git tree inventory",
            )
        planned.append(
            PlannedTree(tracked, name, source, destination, tree_plan, decision)
        )
    return tuple(planned)


def _ownership_destinations(
    tracked: TrackedFile, destination: Path
) -> tuple[Path, ...]:
    """Return every content/topology leaf mutated by one tracked-file entry."""
    if tracked.symlink is None:
        return (destination,)
    target = Path(tracked.symlink).expanduser()
    return tuple(dict.fromkeys((target, destination)))


def _tree_checkpoint_paths(tree: PlannedTree, profile: str) -> tuple[Path, ...]:
    """Return every entry that the frozen tree plan may mutate."""
    relative = {
        entry.path
        for inventory in (
            tree.plan.desired.inventory,
            tree.plan.live,
            tree.plan.prior,
        )
        if inventory is not None
        for entry in inventory.entries
    }
    lock_target = _tree_lock_target(tree.destination)
    root_prefixes = [lock_target]
    current = lock_target
    for part in tree.destination.absolute().relative_to(lock_target).parts:
        current /= part
        root_prefixes.append(current)
    entry_paths = [tree.destination / path for path in sorted(relative)]
    temporary_paths = [
        path.with_name(temporary_entry_name(path.name, purpose))
        for path in entry_paths
        for purpose in ("create", "update", "remove")
    ]
    return tuple(
        dict.fromkeys(
            (
                *root_prefixes,
                *entry_paths,
                *temporary_paths,
                inventory_path(profile, tree.name),
            )
        )
    )


def _tree_lock_target(destination: Path) -> Path:
    """Return the highest missing ancestor whose parent is stable and present."""
    candidate = destination.absolute()
    while not candidate.parent.exists():
        candidate = candidate.parent
    return candidate


def _file_ownership_authorization(
    tracked_entries: tuple[tuple[TrackedFile, str, Path, Path], ...],
    decisions: tuple[FileDecision, ...],
) -> dict[str, bool]:
    """Map logical names while leaving declared symlink topology to deploy."""
    by_locator = {decision.observation.locator: decision for decision in decisions}
    return {
        name: all(
            by_locator[str(path.absolute())].action
            not in {FileAction.ADOPT, FileAction.HOLD}
            for path in _ownership_destinations(tracked, destination)
        )
        for tracked, name, _source, destination in tracked_entries
    }


def _has_protected_units(profile: str, name: str) -> bool:
    """Return whether removal would discard host-only or undecided intent."""
    from setforge.reconcile import store as reconcile_store

    entry = reconcile_store.read_index(profile).files.get(name)
    return bool(
        entry
        and any(
            row.get("cls") in {"local", "pending", "shared_drafted"}
            for row in entry.hunks
        )
    )


def _assert_plan_inputs_unchanged(plan: InstallPlan) -> None:
    """Refuse if source or live inputs changed before the first write."""
    if tuple(_iter_all_tracked_files(plan.ctx)) != plan.tracked_entries:
        raise SetforgeError("tracked file inventory changed after planning; retry")
    current_trees = _plan_trees(
        tuple(_iter_all_trees(plan.ctx)),
        profile=plan.ctx.profile,
        owner_id=plan.package_owner_id,
    )
    if current_trees != plan.trees:
        raise SetforgeError("managed tree inputs changed after planning; retry")
    changed = [
        path
        for path, payload in plan.source_bytes
        if (path.read_bytes() if path.is_file() else None) != payload
    ]
    if changed:
        names = ", ".join(str(path) for path in changed)
        raise SetforgeError(f"install inputs changed after planning: {names}; retry")
    generated_changed: list[str] = []
    for record in plan.deploys:
        spec = record.tracked_file.generated
        if record.generated is None or spec is None:
            continue
        if (
            resolve_generated(record.sub_src.read_text(encoding="utf-8"), spec)
            != record.generated
        ):
            generated_changed.append(record.sub_name)
    if generated_changed:
        names = ", ".join(generated_changed)
        raise SetforgeError(
            f"generated host inputs changed after planning: {names}; retry"
        )
    _assert_live_paths_unchanged(plan.live_paths)
    if transitions.snapshot_paths(plan.dst_paths) != dict(plan.file_pre):
        raise SetforgeError("live install targets changed after planning; retry")
    if transitions.snapshot_paths(plan.ownership_pre) != dict(plan.ownership_pre):
        raise SetforgeError("file ownership changed after planning; retry")


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


def _apply_codex_plugin_plan(
    plan: InstallPlan,
) -> tuple[transitions.CodexPluginDelta | None, tuple[tuple[str, str], ...]]:
    if plan.codex_plugins is None:
        return None, ()
    report = codex_plugins_mod.apply_plan(plan.codex_plugins)
    delta = transitions.CodexPluginDelta(
        installed=tuple(report.installed),
        removed=tuple(report.removed),
        marketplaces_added=tuple(report.marketplaces_added),
        marketplaces_removed=tuple(report.marketplaces_removed),
    )
    return (None if delta.is_empty() else delta), tuple(report.failed)


def _apply_codex_config_plans(
    plans: tuple[codex_resources_mod.CodexConfigPlan, ...],
    *,
    profile: str,
    mutation_guards: MutationLockGuards,
) -> None:
    """Publish native config leaves through their descriptor-bound parents."""
    for plan in plans:
        guard = next(
            (
                item
                for item in mutation_guards.targets
                if item.target.absolute() == plan.destination.parent.absolute()
            ),
            None,
        )
        if guard is None:
            raise SetforgeError("Codex config target lock is missing")
        guard.verify_expected()
        if guard.target_fd is None:
            guard.mkdir(mode=0o700)
        anchor_fd = guard.target_fd
        if anchor_fd is None:  # pragma: no cover - guard mkdir invariant
            raise SetforgeError("Codex config target lock has no descriptor")

        def write_config(
            path: Path, data: bytes, *, parent_fd: int = anchor_fd
        ) -> None:
            atomicio.atomic_write_bytes_at(parent_fd, path.name, data)

        codex_resources_mod.apply_config_plan(
            plan,
            write=write_config,
            record_base=lambda resource_id, data: reconcile_store.write_base(
                profile, file_id(resource_id), data
            ),
            record_marker=lambda resource_id, data: reconcile_store.write_base(
                profile, file_id(resource_id), data
            ),
        )
        guard.verify_expected()


def _apply_capability_targets(  # noqa: C901 - one closure per frozen target phase
    plan: InstallPlan,
    journal: operations.OperationJournal,
    *,
    profile: str,
    active_lock: LockFile | None,
    tracked_checkpoint_paths: tuple[Path, ...],
    adapter_kinds: set[operations.AdapterKind],
    retry_failed: bool,
    yes: bool,
    mutation_guards: MutationLockGuards,
) -> _CapabilityApplyResult:
    """Apply frozen target plans in the selected bundles' graph order."""
    cfg = plan.ctx.cfg
    resolved = plan.ctx.resolved
    provision_results: tuple[ReconcileResult, ...] = ()
    deploy_outcome: install_helpers_mod.DeployOutcome | None = None
    seeded: tuple[str, ...] = ()
    ext_delta: transitions.ExtensionDelta | None = None
    ext_outcomes: tuple[transitions.ReconcileOutcome, ...] = ()
    plugin_delta: transitions.PluginDelta | None = None
    plugin_outcomes: tuple[transitions.ReconcileOutcome, ...] = ()
    codex_plugin_delta: transitions.CodexPluginDelta | None = None
    codex_plugin_failed: tuple[tuple[str, str], ...] = ()
    retry_failed_ids = (
        _collect_retry_failed_ids(profile) if retry_failed else frozenset()
    )

    def apply_packages() -> CapabilityActivation:
        nonlocal journal, provision_results
        has_work = _provisioning_plan_has_work(plan.provisioning)
        claim_paths = tuple(
            OwnershipStore().claim_path(decision.resource_id)
            for decision in plan.provisioning.ownership
            if decision.action in {PackageAction.INSTALL, PackageAction.UPGRADE}
        )
        if has_work:
            journal = operations.begin_checkpoint(
                journal,
                name="packages",
                kind=operations.CheckpointKind.IRREVERSIBLE,
                recovery=(
                    "inspect package-manager output and receipts; SetForge will not "
                    "guess an uninstall for potentially user-owned software"
                ),
                paths=claim_paths,
                restore_state=False,
                restore_transitions=False,
                adapters=(),
            )
        provision_results = tuple(
            reconcile_packages(cfg, resolved, lock=active_lock, plan=plan.provisioning)
        )
        if any(
            outcome.outcome is Outcome.OK
            for result in provision_results
            for outcome in result.outcomes
        ):
            owner_id = _package_owner_id(plan)
            if owner_id is not None:
                publish_installed_package_claims_locked(
                    plan.provisioning, provision_results, owner_id=owner_id
                )
        if has_work:
            journal = operations.finish_checkpoint(journal)
        return CapabilityActivation(
            status=(
                CapabilityStatus.FAILED
                if has_hard_failure(provision_results)
                and bool(plan.provisioning.bundle_graphs)
                else CapabilityStatus.ACTIVE
            ),
            changed=any(not result.delta.is_empty() for result in provision_results),
            detail="package provisioning reported a hard failure"
            if has_hard_failure(provision_results)
            else "",
        )

    def apply_files() -> CapabilityActivation:
        nonlocal journal, deploy_outcome, seeded
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
        codex_resources_mod.assert_projects_trusted(plan.codex_trusted_projects)
        deploy_outcome = install_helpers_mod._apply_tracked_file_plan(
            profile,
            plan.deploys,
        )
        _apply_codex_config_plans(
            plan.codex_configs,
            profile=profile,
            mutation_guards=mutation_guards,
        )
        for tree in plan.trees:
            guard = next(
                (
                    item
                    for item in mutation_guards.targets
                    if item.target.absolute() == _tree_lock_target(tree.destination)
                ),
                None,
            )
            if guard is None:
                raise SetforgeError("managed tree target lock is missing")
            guard.verify_expected()
            if guard.target_fd is None:
                guard.mkdir()
            anchor_fd = guard.target_fd
            if anchor_fd is None:  # pragma: no cover - guard mkdir invariant
                raise SetforgeError("managed tree target lock has no descriptor")
            if tree.decision.action is FileAction.ADOPT:
                inventory = replace(
                    tree.plan.live,
                    owned_paths=tuple(entry.path for entry in tree.plan.live.entries),
                )
            else:
                policy = tree.tracked_file.tree
                if policy is None:  # pragma: no cover - frozen plan invariant
                    raise SetforgeError("managed tree lost its policy")
                inventory = apply_tree(
                    tree.plan,
                    tree.destination,
                    policy,
                    anchor_fd=anchor_fd,
                    anchor_relative=tree.destination.absolute()
                    .relative_to(guard.target.absolute())
                    .parts,
                )
            guard.verify_expected()
            write_inventory(profile, tree.name, inventory)
        seeded = tuple(
            host_local_record.seed_section_slots_to_store(
                cfg, resolved, plan.ctx.repo_root, profile
            )
        )
        if seeded:
            typer.secho(
                f"seeded host-local section template(s): {', '.join(sorted(seeded))}",
                err=True,
                fg=typer.colors.GREEN,
            )
        journal = operations.finish_checkpoint(journal)
        return CapabilityActivation(status=CapabilityStatus.ACTIVE, changed=True)

    def apply_extensions() -> CapabilityActivation:
        nonlocal journal, ext_delta, ext_outcomes
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
        failed = any(
            outcome.status in {ReconcileStatus.SKIPPED, ReconcileStatus.ABORTED}
            for outcome in ext_outcomes
        )
        return CapabilityActivation(
            status=(
                CapabilityStatus.FAILED
                if failed and bool(plan.provisioning.bundle_graphs)
                else CapabilityStatus.ACTIVE
            ),
            changed=ext_delta is not None,
            detail="extension reconciliation left a capability inactive"
            if failed
            else "",
        )

    def apply_plugins() -> CapabilityActivation:
        nonlocal journal, plugin_delta, plugin_outcomes
        nonlocal codex_plugin_delta, codex_plugin_failed
        journal = operations.begin_checkpoint(
            journal,
            name="plugins-and-marketplaces",
            kind=operations.CheckpointKind.COMPENSATABLE,
            recovery="restore frozen plugin and marketplace inventories",
            paths=(),
            restore_state=False,
            restore_transitions=False,
            adapters=tuple(
                kind
                for kind in (
                    operations.AdapterKind.PLUGINS,
                    operations.AdapterKind.CODEX_PLUGINS,
                )
                if kind in adapter_kinds
            ),
        )
        plugin_delta, plugin_outcomes = _apply_plugin_plan(
            plan,
            retry_failed_ids=retry_failed_ids,
            yes=yes,
            lock=active_lock,
        )
        codex_plugin_delta, codex_plugin_failed = _apply_codex_plugin_plan(plan)
        journal = operations.finish_checkpoint(journal)
        failed = bool(codex_plugin_failed) or any(
            outcome.status in {ReconcileStatus.SKIPPED, ReconcileStatus.ABORTED}
            for outcome in plugin_outcomes
        )
        return CapabilityActivation(
            status=(
                CapabilityStatus.FAILED
                if failed and bool(plan.provisioning.bundle_graphs)
                else CapabilityStatus.ACTIVE
            ),
            changed=plugin_delta is not None,
            detail="plugin reconciliation left a capability inactive" if failed else "",
        )

    phase_nodes = tuple(
        CapabilityNode(f"@profile:{kind.value}", kind, ())
        for kind in (
            CapabilityTargetKind.PACKAGE,
            CapabilityTargetKind.FILE,
            CapabilityTargetKind.EXTENSION,
            CapabilityTargetKind.PLUGIN,
        )
    )
    graph = CapabilityGraph((*phase_nodes, *plan.provisioning.capability_graph.nodes))
    activators = {
        CapabilityTargetKind.PACKAGE: apply_packages,
        CapabilityTargetKind.FILE: apply_files,
        CapabilityTargetKind.EXTENSION: apply_extensions,
        CapabilityTargetKind.PLUGIN: apply_plugins,
    }
    outcomes = graph.execute(
        tuple(
            CapabilityTargetAction(kind, lambda: None, activators[kind])
            for kind in activators
        )
    )
    if plan.provisioning.bundle_graphs:
        typer.echo(
            "capabilities: "
            + ", ".join(
                f"{outcome.target_kind.value}={outcome.status.value}"
                for outcome in outcomes
            )
        )
    failed = tuple(
        outcome
        for outcome in outcomes
        if outcome.status
        in {
            CapabilityStatus.FAILED,
            CapabilityStatus.BLOCKED,
            CapabilityStatus.RECOVERY_REQUIRED,
        }
    )
    if failed and plan.provisioning.bundle_graphs:
        summary = ", ".join(
            f"{outcome.target_kind.value}={outcome.status.value}" for outcome in failed
        )
        raise SetforgeError(f"capability graph activation failed: {summary}")
    if deploy_outcome is None:  # pragma: no cover - profile file phase is synthetic
        raise AssertionError("capability graph omitted the tracked-file target")
    return _CapabilityApplyResult(
        journal=journal,
        provision_results=tuple(provision_results),
        deploy_outcome=deploy_outcome,
        seeded=seeded,
        ext_delta=ext_delta,
        ext_outcomes=ext_outcomes,
        plugin_delta=plugin_delta,
        plugin_outcomes=plugin_outcomes,
        codex_plugin_delta=codex_plugin_delta,
        codex_plugin_failed=codex_plugin_failed,
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
    if plan.codex_plugins is not None:
        report = codex_plugins_mod.apply_plan(plan.codex_plugins, dry_run=True)
        typer.echo("=== would-be Codex plugin reconcile ===")
        for name in report.marketplaces_added:
            typer.echo(f"  WOULD add marketplace  {name}")
        for name, _source in report.marketplaces_removed:
            typer.echo(f"  WOULD remove marketplace  {name}")
        for plugin_id in report.installed:
            typer.echo(f"  WOULD install  {plugin_id}")
        for plugin_id in report.removed:
            typer.echo(f"  WOULD remove  {plugin_id}")
        if not report:
            typer.echo("  nothing to reconcile")


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


def _confirm_package_adoptions(
    decisions: tuple[PackageDecision, ...], *, yes: bool
) -> None:
    """Confirm metadata-only ownership claims before the first effect."""
    decisions = tuple(
        decision for decision in decisions if decision.action is PackageAction.ADOPT
    )
    if not decisions or yes:
        return
    names = ", ".join(decision.item.identity.display for decision in decisions)
    if not sys.stdin.isatty():
        raise SetforgeError(
            f"package adoption requires confirmation for {names}; rerun with --yes"
        )
    if not typer.confirm(
        f"Manage existing package(s) without reinstalling: {names}?",
        default=False,
    ):
        raise SetforgeError("package adoption declined; no package changes applied")


def _confirm_file_adoptions(decisions: tuple[FileDecision, ...], *, yes: bool) -> None:
    """Confirm container claims separately from reconcile content choices."""
    adopt = tuple(
        decision for decision in decisions if decision.action is FileAction.ADOPT
    )
    blocked = tuple(
        decision for decision in decisions if decision.action is FileAction.HOLD
    )
    if blocked:
        names = ", ".join(decision.observation.locator for decision in blocked)
        raise SetforgeError(f"tracked file ownership blocks install for {names}")
    if not adopt or yes:
        return
    names = ", ".join(decision.observation.locator for decision in adopt)
    if not sys.stdin.isatty():
        raise SetforgeError(
            f"file adoption requires confirmation for {names}; rerun with --yes"
        )
    if not typer.confirm(
        f"Manage existing tracked file(s) without replacing them first: {names}?",
        default=False,
    ):
        raise SetforgeError("file adoption declined; no file changes applied")


def _prepare_package_owner_id(
    repo_root: Path,
    decisions: tuple[PackageDecision, ...],
    *,
    required: bool = False,
) -> UUID | None:
    """Mint the checkout owner before the lower-ranked install lock scope."""
    if not decisions and not required:
        return None
    try:
        return load_or_create_owner_id(repo_root)
    except OwnershipError:
        return None


def _preview_file_ownership(config: Path, profile: str) -> tuple[FileDecision, ...]:
    """Build the file consent surface without holding mutation locks."""
    cfg = load_config(config)
    resolved = resolve_effective_profile(cfg, profile, config.parent).resolved
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=config.parent, profile=profile
    )
    try:
        owner_id = read_owner_id(config.parent)
    except OwnershipError:
        owner_id = None
    regular = _plan_file_ownership(
        tuple(_iter_all_tracked_files(ctx)), profile=profile, owner_id=owner_id
    )
    trees = _plan_trees(tuple(_iter_all_trees(ctx)), profile=profile, owner_id=owner_id)
    return regular + tuple(tree.decision for tree in trees)


def _preview_tree_targets(config: Path, profile: str) -> tuple[Path, ...]:
    """Resolve explicit filesystem roots for mutation-lock acquisition."""
    cfg = load_config(config)
    resolved = resolve_effective_profile(cfg, profile, config.parent).resolved
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=config.parent, profile=profile
    )
    roots = {
        *(
            _tree_lock_target(destination)
            for *_prefix, destination in _iter_all_trees(ctx)
        ),
        *codex_resources_mod.config_target_roots(
            cfg,
            resolved,
            config.parent,
            stored_ids=tuple(map(str, reconcile_store.stored_file_ids(profile))),
        ),
    }
    return tuple(sorted(roots, key=str))


def _read_package_owner_id(repo_root: Path) -> UUID | None:
    """Read an established owner without making dry-run metadata changes."""
    try:
        return read_owner_id(repo_root)
    except OwnershipError:
        return None


def _publish_package_adoptions(plan: InstallPlan) -> None:
    """Publish confirmed claims after exact locked plan revalidation."""
    decisions = tuple(
        decision
        for decision in plan.provisioning.ownership
        if decision.action is PackageAction.ADOPT
    )
    if not decisions:
        return
    owner_id = plan.package_owner_id
    if owner_id is None:
        typer.secho(
            "warning: existing packages remain unowned because the configuration "
            "is not Git-backed",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return
    store = OwnershipStore()
    receipts = ReceiptStore(default_receipt_root())
    for decision in decisions:
        if store.read(decision.resource_id) != decision.claim:
            raise SetforgeError("package ownership changed after confirmation; retry")
        claimed_decision = decision
        if (
            decision.observation is not None
            and decision.observation.origin is ObservationOrigin.LEGACY_RECEIPT
        ):
            receipts.migrate_legacy(decision.item.identity, provider=decision.item.type)
            claimed_decision = replace(
                decision,
                observation=replace(
                    decision.observation, origin=ObservationOrigin.CURRENT_RECEIPT
                ),
            )
        publish_claim_locked(
            store,
            claimed_decision,
            owner_id=owner_id,
            declaration_ref=(
                f"packages.{decision.item.type}.{decision.item.identity.key}"
            ),
            acquisition="adopted-external",
        )
        typer.echo(
            f"adopted package ownership: {decision.item.identity.display} "
            "(no package bytes changed)"
        )


def _publish_adoptions_checkpoint(
    plan: InstallPlan, journal: operations.OperationJournal
) -> operations.OperationJournal:
    """Journal and publish metadata-only adoption claims."""
    claim_paths = tuple(
        OwnershipStore().claim_path(decision.resource_id)
        for decision in plan.provisioning.ownership
        if decision.action is PackageAction.ADOPT
    )
    if not claim_paths:
        return journal
    receipt_paths = _legacy_adoption_receipt_paths(plan)
    applying = operations.begin_checkpoint(
        journal,
        name="package-adoption",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore package ownership claims and migrated receipts",
        paths=(*claim_paths, *receipt_paths),
        restore_state=False,
        restore_transitions=False,
        adapters=(),
    )
    _publish_package_adoptions(plan)
    return operations.finish_checkpoint(applying)


def _publish_file_claims(
    plan: InstallPlan,
    *,
    actions: frozenset[FileAction],
    refresh: bool = False,
) -> None:
    """Publish exact file claims at the appropriate side of file effects."""
    owner_id = plan.package_owner_id
    if owner_id is None:
        if any(decision.action in actions for decision in plan.file_ownership):
            typer.secho(
                "warning: tracked files remain without durable ownership because "
                "the configuration is not Git-backed",
                err=True,
                fg=typer.colors.YELLOW,
            )
        return
    store = OwnershipStore()
    by_resource = {
        decision.observation.resource_id: decision
        for decision in plan.file_ownership
        if decision.action in actions
    }
    declarations = {
        str(path.absolute()): name
        for tracked, name, _source, destination in plan.tracked_entries
        for path in _ownership_destinations(tracked, destination)
    }
    declarations.update(
        {str(tree.destination.absolute()): tree.name for tree in plan.trees}
    )
    trees_by_locator = {str(tree.destination.absolute()): tree for tree in plan.trees}
    for resource_id, expected in by_resource.items():
        current = store.read(resource_id)
        if current != expected.claim and not (
            refresh
            and expected.action is FileAction.ADOPT
            and expected.claim is None
            and current is not None
            and current.owner_id == owner_id
        ):
            raise SetforgeError(
                "tracked file ownership changed after confirmation; retry"
            )
        tree = trees_by_locator.get(expected.observation.locator)
        if tree is None:
            observed = observe_file(
                Path(expected.observation.locator),
                allow_topology=expected.observation.topology,
            )
        else:
            policy = tree.tracked_file.tree
            if policy is None:  # pragma: no cover - frozen plan invariant
                raise SetforgeError("managed tree lost its policy")
            live = scan_tree(
                tree.destination,
                policy.model_copy(update={"symlinks": TreeSymlinkPolicy.PRESERVE}),
            ).inventory
            observed = observe_tree(tree.destination, live.fingerprint)
        if observed.resource_id != resource_id:
            if current is not None:
                current = store.move_locked(
                    resource_id,
                    observed.resource_id,
                    expected_owner=owner_id,
                    expected_generation=current.generation,
                )
            resource_id = observed.resource_id
        if current is not None and current.fingerprint == observed.fingerprint:
            continue
        locked = decide_file(observed, current, owner_id=owner_id)
        publish_file_claim_locked(
            store,
            locked,
            owner_id=owner_id,
            declaration_ref=(
                f"tracked_files.{declarations[expected.observation.locator]}"
            ),
            acquisition=(
                "adopted-external"
                if expected.action is FileAction.ADOPT
                else "setforge-installed"
                if expected.action is FileAction.INSTALL
                else "observed-local"
            ),
            provenance=_generated_file_provenance(plan, expected.observation.locator),
        )


def _generated_file_provenance(
    plan: InstallPlan, locator: str
) -> tuple[ProvenanceFact, ...]:
    """Return generator facts only for the generated content container."""
    for record in plan.deploys:
        if record.generated is None:
            continue
        content_path = (
            Path(record.tracked_file.symlink).expanduser()
            if record.tracked_file.symlink is not None
            else record.sub_dst
        )
        if str(content_path.absolute()) != locator:
            continue
        return (
            ProvenanceFact(ProvenanceFactKind.GENERATOR, "jinja2"),
            ProvenanceFact(
                ProvenanceFactKind.INTEGRITY,
                f"generated-spec-sha256:{record.generated.fingerprint}",
            ),
            *(
                ProvenanceFact(
                    ProvenanceFactKind.RESOLVER,
                    f"{name}:{kind.value}={value}",
                )
                for name, kind, value in record.generated.inputs
            ),
        )
    return ()


def _publish_file_adoptions_checkpoint(
    plan: InstallPlan, journal: operations.OperationJournal
) -> operations.OperationJournal:
    claim_paths = tuple(
        OwnershipStore().claim_path(decision.observation.resource_id)
        for decision in plan.file_ownership
        if decision.action is FileAction.ADOPT
    )
    if not claim_paths:
        return journal
    applying = operations.begin_checkpoint(
        journal,
        name="file-adoption",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore tracked-file ownership claims",
        paths=claim_paths,
        restore_state=False,
        restore_transitions=False,
        adapters=(),
    )
    _publish_file_claims(plan, actions=frozenset({FileAction.ADOPT}))
    return operations.finish_checkpoint(applying)


def _refresh_file_claims_checkpoint(
    plan: InstallPlan, journal: operations.OperationJournal
) -> operations.OperationJournal:
    """Refresh every successful file effect, including identity transitions."""
    decisions = tuple(
        decision
        for decision in plan.file_ownership
        if decision.action
        in {
            FileAction.ADOPT,
            FileAction.INSTALL,
            FileAction.MANAGE,
            FileAction.REVIEW,
        }
    )
    if not decisions or plan.package_owner_id is None:
        return journal
    store = OwnershipStore()
    paths = tuple(
        dict.fromkeys(
            path
            for decision in decisions
            for path in (
                store.claim_path(decision.observation.resource_id),
                store.claim_path(
                    observe_file(
                        Path(decision.observation.locator),
                        allow_topology=decision.observation.topology,
                    ).resource_id
                ),
            )
        )
    )
    journal = operations.extend_paths(journal, paths)
    applying = operations.begin_checkpoint(
        journal,
        name="file-ownership-refresh",
        kind=operations.CheckpointKind.REVERSIBLE,
        recovery="restore tracked-file ownership claims",
        paths=paths,
        restore_state=False,
        restore_transitions=False,
        adapters=(),
    )
    _publish_file_claims(
        plan,
        actions=frozenset(
            {
                FileAction.ADOPT,
                FileAction.INSTALL,
                FileAction.MANAGE,
                FileAction.REVIEW,
            }
        ),
        refresh=True,
    )
    return operations.finish_checkpoint(applying)


def _legacy_adoption_receipt_paths(plan: InstallPlan) -> tuple[Path, ...]:
    """Return both old and new receipt paths for journaled adoption migration."""
    receipts = ReceiptStore(default_receipt_root())
    return tuple(
        path
        for decision in plan.provisioning.ownership
        if decision.action is PackageAction.ADOPT
        and decision.observation is not None
        and decision.observation.origin is ObservationOrigin.LEGACY_RECEIPT
        for path in (
            receipts.receipt_path(decision.item.identity, provider=None),
            receipts.receipt_path(decision.item.identity, provider=decision.item.type),
        )
    )


def _preview_package_ownership(
    config: Path, profile: str, *, locked: bool
) -> tuple[PackageDecision, ...]:
    """Build the consent surface without holding mutation locks."""
    cfg = load_config(config)
    resolved = resolve_effective_profile(cfg, profile, config.parent).resolved
    direct_items = resolve_provision_items(cfg, resolved)
    bundle_items = tuple(
        item
        for name in resolved.bundles
        for item in resolve_bundle_items(cfg.bundles[name], cfg)
    )
    if not direct_items and not bundle_items:
        return ()
    active_lock = _prepare_lock(config, cfg, resolved, locked=locked)
    try:
        owner_id = read_owner_id(config.parent)
    except OwnershipError:
        owner_id = None
    return plan_provisioning(
        cfg,
        resolved,
        lock=active_lock,
        ownership_store=OwnershipStore(),
        owner_id=owner_id,
    ).ownership


def _package_owner_id(plan: InstallPlan):
    """Return the config owner, preserving non-Git installs as unverified."""
    if plan.package_owner_id is None:
        typer.secho(
            "warning: package installed without an ownership claim because the "
            "configuration is not Git-backed",
            err=True,
            fg=typer.colors.YELLOW,
        )
    return plan.package_owner_id


@app.command(epilog=INSTALL_EXAMPLES)
def install(  # noqa: C901 - confirmation and frozen-plan orchestration
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
        package_owner_id = _read_package_owner_id(repo_root)
        plan = _build_install_plan(
            ctx,
            section_auto=section_auto,
            interactive=False,
            lock=active_lock,
            transition=not no_transition,
            input_baseline=input_baseline,
            auto=True,
            package_owner_id=package_owner_id,
        )
        scan_result = secrets_mod.run_pre_deploy_scan(
            tracked_root=config.parent / "tracked",
            skip=no_secrets_scan,
        )
        _render_install_plan(plan, scan_result)
        return

    ownership_config = config.read_bytes()
    ownership_preview = _preview_package_ownership(config, profile, locked=locked)
    file_ownership_preview = _preview_file_ownership(config, profile)
    tree_target_preview = _preview_tree_targets(config, profile)
    if config.read_bytes() != ownership_config:
        raise SetforgeError("install configuration changed while loading; retry")
    _confirm_package_adoptions(ownership_preview, yes=yes)
    if (repo_root / ".git").exists():
        _confirm_file_adoptions(file_ownership_preview, yes=yes)
    package_owner_id = _prepare_package_owner_id(
        repo_root,
        ownership_preview,
        required=bool(file_ownership_preview),
    )

    with (
        mutation_locks(
            resources=True,
            config_dir=config.parent,
            target_roots=tree_target_preview,
            profile=profile,
        ) as mutation_guards,
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
            package_owner_id=package_owner_id,
        )
        planned_target_roots = {
            *(_tree_lock_target(tree.destination) for tree in plan.trees),
            *(codex.destination.parent for codex in plan.codex_configs),
        }
        if tuple(sorted(planned_target_roots, key=str)) != tree_target_preview:
            raise SetforgeError(
                "managed tree targets changed after confirmation; retry"
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
        if plan.provisioning.ownership != ownership_preview:
            raise SetforgeError(
                "package ownership inputs changed after confirmation; retry"
            )
        if plan.file_ownership != file_ownership_preview:
            confirmation_actions = {FileAction.ADOPT, FileAction.HOLD}
            if any(
                decision.action in confirmation_actions
                for decision in (*file_ownership_preview, *plan.file_ownership)
            ):
                raise SetforgeError(
                    "file ownership inputs changed after confirmation; retry"
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

        deploy_state_pre = install_helpers_mod._capture_store_snapshots(
            profile, plan.deploys
        )
        captured_base_keys = {
            entry.key
            for entry in deploy_state_pre
            if entry.store is transitions.SnapshotStore.BASE
        }
        state_pre = (
            *deploy_state_pre,
            *(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.BASE,
                    profile,
                    codex_plan.resource_id,
                )
                for codex_plan in plan.codex_configs
                if codex_plan.resource_id not in captured_base_keys
            ),
            *(
                transitions.snapshot_store_state(
                    transitions.SnapshotStore.BASE,
                    profile,
                    codex_plan.mcp_marker_id,
                )
                for codex_plan in plan.codex_configs
                if codex_plan.mcp_marker_id is not None
                and codex_plan.mcp_marker_id not in captured_base_keys
            ),
        )
        secrets_checkpoint_paths = (
            *plan.bootstrap,
            *((secret_plan.allowlist_path,) if secret_plan.hashes else ()),
        )
        ownership_paths = tuple(
            OwnershipStore().claim_path(decision.resource_id)
            for decision in plan.provisioning.ownership
            if decision.action
            in {PackageAction.ADOPT, PackageAction.INSTALL, PackageAction.UPGRADE}
        )
        file_ownership_paths = tuple(
            OwnershipStore().claim_path(decision.observation.resource_id)
            for decision in plan.file_ownership
            if decision.action
            in {
                FileAction.ADOPT,
                FileAction.INSTALL,
                FileAction.MANAGE,
                FileAction.REVIEW,
            }
        )
        adoption_receipt_paths = _legacy_adoption_receipt_paths(plan)
        journal_paths = tuple(
            dict.fromkeys(
                (
                    *plan.dst_paths,
                    *(sub_dst for _, _, _, sub_dst in plan.tracked_entries),
                    *(
                        path
                        for tree in plan.trees
                        for path in _tree_checkpoint_paths(tree, profile)
                    ),
                    *secrets_checkpoint_paths,
                    *ownership_paths,
                    *file_ownership_paths,
                    *adoption_receipt_paths,
                )
            )
        )
        tracked_checkpoint_paths = tuple(
            dict.fromkeys(
                (
                    *plan.dst_paths,
                    *(sub_dst for _, _, _, sub_dst in plan.tracked_entries),
                    *(
                        path
                        for tree in plan.trees
                        for path in _tree_checkpoint_paths(tree, profile)
                    ),
                    *(
                        OwnershipStore().claim_path(decision.observation.resource_id)
                        for decision in plan.file_ownership
                        if decision.action
                        in {FileAction.INSTALL, FileAction.MANAGE, FileAction.REVIEW}
                    ),
                )
            )
        )
        tree_filesystem_paths = tuple(
            dict.fromkeys(
                path
                for tree in plan.trees
                for path in _tree_checkpoint_paths(tree, profile)
            )
        )
        tree_pre_images = {
            path: transitions.snapshot_filesystem_image(path)
            for path in tree_filesystem_paths
        }
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
        journal = _publish_adoptions_checkpoint(plan, journal)
        journal = _publish_file_adoptions_checkpoint(plan, journal)

        # For symlink-deployed tracked_files the recorded "touched path" is
        # the symlink's TARGET (where bytes actually land), not the link
        # path itself: GNU patch refuses to patch a symlink as a regular
        # file, so a transition recording the link path would brick revert.
        dst_paths = [*plan.dst_paths, *plan.ownership_pre]
        # Store files (byte bases, spans sidecars, scalar-base manifests) do
        # NOT ride this patch snapshot: their pre-install state is captured
        # at the pass-2 barrier (state_snapshots below) and revert restores
        # them through that mechanism — recording them here too would
        # double-restore (Invariant I5 now lives in the snapshot path).

        file_pre = {**plan.file_pre, **plan.ownership_pre}

        capability_result = _apply_capability_targets(
            plan,
            journal,
            profile=profile,
            active_lock=active_lock,
            tracked_checkpoint_paths=tracked_checkpoint_paths,
            adapter_kinds=adapter_kinds,
            retry_failed=retry_failed,
            yes=yes,
            mutation_guards=mutation_guards,
        )
        journal = capability_result.journal
        provision_results = list(capability_result.provision_results)
        deploy_outcome = capability_result.deploy_outcome
        seeded = capability_result.seeded
        ext_delta = capability_result.ext_delta
        ext_outcomes = capability_result.ext_outcomes
        plugin_delta = capability_result.plugin_delta
        plugin_outcomes = capability_result.plugin_outcomes
        codex_plugin_delta = capability_result.codex_plugin_delta
        codex_plugin_failed = capability_result.codex_plugin_failed
        journal = _refresh_file_claims_checkpoint(plan, journal)
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
        tree_post_images = {
            path: transitions.snapshot_filesystem_image(path)
            for path in tree_filesystem_paths
        }
        tree_filesystem_deltas = tuple(
            transitions.FilesystemDelta(
                path,
                tree_pre_images[path],
                tree_post_images[path],
            )
            for path in tree_filesystem_paths
            if tree_pre_images[path] != tree_post_images[path]
        )

        _emit_reconcile_summary(plugin_outcomes, ext_outcomes)

        if not no_transition and not _install_recorded_nothing(
            file_pre=file_pre,
            file_post=file_post,
            deploy_outcome=deploy_outcome,
            ext_delta=ext_delta,
            plugin_delta=plugin_delta,
            codex_plugin_delta=codex_plugin_delta,
            mcp_delta=mcp_delta,
            reconcile_outcomes=plugin_outcomes + ext_outcomes,
            seeded=bool(seeded),
            codex_base_mutated=any(
                entry.store is transitions.SnapshotStore.BASE
                and entry.key.startswith(("codex/config/", "codex/mcp-target/"))
                and transitions.snapshot_store_state(
                    entry.store, entry.profile, entry.key
                )
                != entry
                for entry in state_pre
            ),
            filesystem_deltas=tree_filesystem_deltas,
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
                codex_plugin_delta=codex_plugin_delta,
                source_dir=ctx.repo_root,
                reconcile_outcomes=plugin_outcomes + ext_outcomes,
                state_snapshots=state_pre,
                mcp_delta=mcp_delta,
                file_modes=deploy_outcome.prior_modes,
                filesystem_deltas=tree_filesystem_deltas,
            )
            typer.echo(f"transition: {target}")
            typer.echo(f"↩  revert with: setforge revert --profile={profile}")
            journal = operations.finish_checkpoint(journal)

        operations.complete(journal)

        _gate_on_mcp_failures(mcp_failed)
        if codex_plugin_failed:
            details = "; ".join(
                f"{item}: {error}" for item, error in codex_plugin_failed
            )
            raise SetforgeError(f"Codex plugin reconciliation failed: {details}")
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
    if plan.codex_plugins is not None:
        snapshots.append(
            operations.AdapterSnapshot(
                operations.AdapterKind.CODEX_PLUGINS,
                json.dumps(
                    {
                        "plugins": list(plan.codex_plugins.pre_plugin_ids),
                        "marketplaces": [
                            list(item) for item in plan.codex_plugins.pre_marketplaces
                        ],
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
