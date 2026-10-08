"""install subcommand — orchestrates tracked-file deploy + extension/plugin reconcile.

Wires deploy.resolve_deploy / deploy.write_resolved_deploy, extension/plugin
reconcile, and the transition snapshot. Imports ``app`` from
:mod:`setforge.cli` so the ``@app.command()`` registration fires at
module import time; ``setforge/cli/__init__.py`` imports this module at
the bottom for the side effect.
"""

from __future__ import annotations

import difflib
import json
import os
import stat
import sys
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID, uuid4

import typer

from setforge import (
    atomicio,
    codex_lifecycle,
    deploy,
    operations,
    paths,
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
    ReconcilePolicy,
    ResolvedProfile,
    TrackedFile,
    load_config,
    refuse_unmigrated_host_local_leak,
    resolve_effective_profile,
)
from setforge.errors import ExtensionToolMissing, PluginToolMissing, SetforgeError
from setforge.file_ownership import (
    FileAction,
    FileDecision,
    decide_file,
    file_resource_id,
    observe_file,
    observe_tree,
    publish_file_claim_locked,
)
from setforge.generated import resolve_generated_file
from setforge.lockfile import LockFile, lock_path, parse_lock
from setforge.locking import MutationLockGuards, TargetLockGuard, mutation_locks
from setforge.ownership import (
    Authority,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipError,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
    load_or_create_owner_id_locked,
    read_owner_id,
    resolve_owner_common_dir,
)
from setforge.ownership_history import OwnershipHistoryStore
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
from setforge.reconcile.types import FileId, file_id
from setforge.secrets import SecretAction, SecretsScanResult
from setforge.transitions import (
    ReconcileStatus,
    load_latest,
    load_reconcile_outcomes,
)
from setforge.tree_management import (
    TreeActionKind,
    TreeEntryKind,
    TreeHoldResolution,
    TreePlan,
    apply_tree,
    holds_only_state_trees,
    inventory_path,
    plan_tree,
    read_inventory,
    scan_live_tree,
    scan_tree,
    state_trees_under,
    temporary_entry_name,
    write_inventory,
)

if TYPE_CHECKING:
    from setforge.cli.stage import StageSummary


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """Read-only install decisions consumed by preview and apply."""

    ctx: ProfileContext
    drift_report: compare_mod.CompareReport
    staging: tuple[StageSummary, ...]
    deploys: tuple[_PendingDeploy, ...]
    reconcile_store_mutation: bool
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
    preserved_store_ids: frozenset[FileId] = frozenset()
    tree_held: TreeHoldResolution | None = None
    symlink_conflicts: tuple[str, ...] = ()

    def tree_paths(self) -> tuple[Path, ...]:
        """Return every path the frozen tree plans may mutate."""
        return tuple(
            path
            for tree in self.trees
            for path in _tree_checkpoint_paths(tree, self.ctx.profile)
        )

    def package_claim_paths(self, *actions: PackageAction) -> tuple[Path, ...]:
        """Return the claim files of package decisions taking one of ``actions``."""
        store = OwnershipStore()
        return tuple(
            store.claim_path(decision.resource_id)
            for decision in self.provisioning.ownership
            if decision.action in actions
        )

    def file_claim_paths(self, *actions: FileAction) -> tuple[Path, ...]:
        """Return the claim files of file decisions taking one of ``actions``."""
        store = OwnershipStore()
        return tuple(
            store.claim_path(decision.observation.resource_id)
            for decision in self.file_ownership
            if decision.action in actions
        )


@dataclass(frozen=True, slots=True)
class PlannedTree:
    """One frozen explicit tree plan and its root authority decision."""

    tracked_file: TrackedFile
    name: str
    source: Path
    destination: Path
    plan: TreePlan
    decision: FileDecision


@dataclass(slots=True)
class _InstallRun:
    """The operation journal and the outputs of the applied install phases."""

    journal: operations.OperationJournal
    provision_results: tuple[ReconcileResult, ...] = ()
    deploy_outcome: install_helpers_mod.DeployOutcome | None = None
    seeded: tuple[str, ...] = ()
    ext_delta: transitions.ExtensionDelta | None = None
    ext_outcomes: tuple[transitions.ReconcileOutcome, ...] = ()
    plugin_delta: transitions.PluginDelta | None = None
    plugin_outcomes: tuple[transitions.ReconcileOutcome, ...] = ()
    codex_plugin_delta: transitions.CodexPluginDelta | None = None
    codex_plugin_failed: tuple[tuple[str, str], ...] = ()

    @contextmanager
    def checkpoint(
        self,
        name: str,
        kind: operations.CheckpointKind,
        *,
        paths: tuple[Path, ...] = (),
        restore_state: bool = False,
        restore_transitions: bool = False,
        adapters: tuple[operations.AdapterKind, ...] = (),
        recovery: str | None = None,
    ) -> Iterator[None]:
        """Journal one effect; an exception leaves it uncertain for recovery."""
        self.journal = operations.begin_checkpoint(
            self.journal,
            name=name,
            kind=kind,
            recovery=recovery,
            paths=paths,
            restore_state=restore_state,
            restore_transitions=restore_transitions,
            adapters=adapters,
        )
        yield
        self.journal = operations.finish_checkpoint(self.journal)


def _provisioning_plan_has_work(plan: ProvisioningPlan) -> bool:
    """Return whether applying the frozen package plan may change the host."""
    if any(not batch.delta.is_empty() for batch in plan.batches):
        return True
    eligible_bundle_items = {
        (decision.item.type, decision.item.identity)
        for decision in plan.ownership
        if decision.action in {PackageAction.INSTALL, PackageAction.UPGRADE}
        and (decision.item.type, decision.item.identity.key) not in plan.direct_keys
    }
    return any(
        (batch.provider_type, identity) in eligible_bundle_items
        for batch in plan.bundle_batches
        for identity in (*batch.delta.installed, *batch.delta.activated)
    )


def _install_plan_recorded_nothing(plan: InstallPlan) -> bool:
    """Plan-side equivalent of ``_install_recorded_nothing``."""
    compared = {entry.name: entry for entry in plan.drift_report.entries}
    file_change = any(
        record.preview_action is not deploy.DeployAction.NOOP
        if record.preview_action is not None
        else compared[record.sub_name].status is not compare_mod.CompareStatus.UNCHANGED
        for record in plan.deploys
    )
    ownership_change = plan.package_owner_id is not None and (
        any(
            decision.action
            in {FileAction.INSTALL, FileAction.ADOPT, FileAction.TRANSFER}
            for decision in plan.file_ownership
        )
        or any(
            decision.action in {PackageAction.ADOPT, PackageAction.TRANSFER}
            for decision in plan.provisioning.ownership
        )
    )
    extension_change = (
        plan.extensions is not None
        and plan.extensions.policy is not ReconcilePolicy.REPORT
        and bool(plan.extensions.to_install or plan.extensions.to_uninstall)
    )
    plugin_change = (
        plan.plugins is not None
        and plan.plugins.policy is not ReconcilePolicy.REPORT
        and bool(
            plan.plugins.to_install
            or plan.plugins.to_enable
            or plan.plugins.to_disable
            or plan.plugins.marketplaces_added
        )
    )
    codex_plugin_change = (
        plan.codex_plugins is not None
        and plan.codex_plugins.policy is not ReconcilePolicy.REPORT
        and bool(
            plan.codex_plugins.to_install
            or plan.codex_plugins.to_remove
            or plan.codex_plugins.marketplaces_to_add
            or plan.codex_plugins.marketplaces_to_replace
        )
    )
    return not (
        file_change
        or plan.reconcile_store_mutation
        or any(tree.plan.changed for tree in plan.trees)
        or any(
            codex.changed or codex.base != codex.desired for codex in plan.codex_configs
        )
        or any(not path.exists() for path in plan.bootstrap)
        or _provisioning_plan_has_work(plan.provisioning)
        or (plan.mcp.value is not None and bool(plan.mcp.value.entries))
        or extension_change
        or plugin_change
        or codex_plugin_change
        or ownership_change
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


def _snapshottable(paths: Iterable[Path]) -> Iterator[Path]:
    """Yield each path the journal can snapshot, skipping any other."""
    for path in paths:
        try:
            image = transitions.capture_filesystem_image(path)
        except OSError:
            continue
        if image is not None:
            yield path


def _journalable_backups(dst_paths: Iterable[Path]) -> Iterator[Path]:
    """Yield each ``.bak`` sibling the journal can snapshot, skipping any other."""
    return _snapshottable(path.with_name(path.name + ".bak") for path in dst_paths)


def _symlink_write_targets(dst_paths: Iterable[Path]) -> tuple[Path, ...]:
    """Return the snapshottable files deploy writes through a user symlink."""
    resolved = {path: deploy._resolve_for_copy(path) for path in dst_paths}
    return tuple(
        _snapshottable(
            dict.fromkeys(target for path, target in resolved.items() if target != path)
        )
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


def _resolve_install_profile(
    cfg: Config,
    profile: str,
    repo_root: Path,
    file_selection: frozenset[str] | None,
) -> tuple[ProfileContext, LocalOverlayResolution]:
    declared = frozenset(cfg.tracked_files)
    effective = resolve_effective_profile(cfg, profile, repo_root)
    if file_selection is not None:
        unavailable = file_selection - (
            declared & frozenset(effective.resolved.tracked_files)
        )
        if unavailable:
            raise SetforgeError(
                "selected tracked file is not a declared member of this profile: "
                + ", ".join(sorted(unavailable))
            )
    return (
        ProfileContext(
            cfg=cfg,
            resolved=effective.resolved,
            repo_root=repo_root,
            profile=profile,
            file_selection=file_selection,
        ),
        effective.local_overlay,
    )


def _load_install_context(
    config: Path,
    profile: str,
    repo_root: Path,
    *,
    locked: bool,
    file_selection: frozenset[str] | None = None,
) -> tuple[
    ProfileContext,
    LockFile | None,
    LocalOverlayResolution,
    tuple[tuple[Path, bytes | None], ...],
]:
    """Load config/overlay/lock from one stable byte snapshot."""
    input_paths = {config.resolve(), paths.local_config_path(), lock_path(config)}
    baseline = _snapshot_inputs(input_paths)
    cfg = load_config(config)
    refuse_unmigrated_host_local_leak(cfg, verb="install", profile=profile)
    ctx, local_overlay = _resolve_install_profile(
        cfg, profile, repo_root, file_selection
    )
    active_lock = (
        _prepare_lock(config, cfg, ctx.resolved, locked=locked)
        if file_selection is None
        else None
    )
    if _snapshot_inputs(input_paths) != baseline:
        raise SetforgeError("install configuration changed while loading; retry")
    return (
        ctx,
        active_lock,
        local_overlay,
        baseline,
    )


def _preserved_file_store_ids(
    ctx: ProfileContext, selected_ids: frozenset[str]
) -> frozenset[FileId]:
    if ctx.file_selection is None:
        return frozenset()
    unselected = set(ctx.resolved.tracked_files) - ctx.file_selection
    ambiguous = {
        fid
        for fid in selected_ids
        if any(fid == name or fid.startswith(name + "/") for name in unselected)
    }
    if ambiguous:
        raise SetforgeError(
            "file-only selection has overlapping tracked-file identities: "
            + ", ".join(sorted(ambiguous))
        )
    return frozenset(
        reconcile_store.stored_file_ids(ctx.profile)
        - {file_id(name) for name in selected_ids}
    )


def _plan(
    config: Path,
    profile: str,
    repo_root: Path,
    *,
    locked: bool,
    file_selection: frozenset[str] | None,
    section_auto: reconcile_apply.ReconcileAuto | None,
    interactive: bool,
    transition: bool,
    auto: bool,
    package_owner_id: UUID | None,
) -> tuple[InstallPlan, LockFile | None, LocalOverlayResolution]:
    """Load one configuration snapshot and freeze every install decision.

    Preview and apply both plan here; apply holds the mutation locks.
    """
    ctx, active_lock, local_overlay, input_baseline = _load_install_context(
        config, profile, repo_root, locked=locked, file_selection=file_selection
    )
    plan = _plan_files(
        ctx,
        section_auto=section_auto,
        interactive=interactive,
        transition=transition,
        input_baseline=input_baseline,
        package_owner_id=package_owner_id,
    )
    if file_selection is None:
        plan = _plan_adapters(plan, lock=active_lock, auto=auto)
    planned_entries = tuple(
        (record.tracked_file, record.sub_name, record.sub_src, record.sub_dst)
        for record in plan.deploys
    )
    expected_names = tuple(
        sub_name for _, sub_name, _, _ in plan.tracked_entries
    ) + tuple(tree.name for tree in plan.trees)
    compared_names = tuple(entry.name for entry in plan.drift_report.entries)
    if (
        planned_entries != plan.tracked_entries
        or tuple(_iter_all_tracked_files(ctx)) != plan.tracked_entries
        or sorted(compared_names) != sorted(expected_names)
    ):
        raise SetforgeError("tracked file inventory changed during planning; retry")
    if _snapshot_inputs({path for path, _ in plan.source_bytes}) != plan.source_bytes:
        raise SetforgeError("install inputs changed during planning; retry")
    _assert_live_paths_unchanged(plan.live_paths)
    if transitions.snapshot_paths(plan.dst_paths) != dict(plan.file_pre):
        raise SetforgeError("live install targets changed during planning; retry")
    if transitions.snapshot_paths(plan.ownership_pre) != dict(plan.ownership_pre):
        raise SetforgeError("file ownership changed during planning; retry")
    return plan, active_lock, local_overlay


def _plan_files(
    ctx: ProfileContext,
    *,
    section_auto: reconcile_apply.ReconcileAuto | None,
    interactive: bool,
    transition: bool,
    input_baseline: tuple[tuple[Path, bytes | None], ...],
    package_owner_id: UUID | None,
) -> InstallPlan:
    """Freeze the tracked-file, tree, native config and bootstrap decisions."""
    from setforge.cli.stage import (
        collect_stages,
        collect_structured_stages,
        summarize_stages,
    )

    tracked_entries = tuple(_iter_all_tracked_files(ctx))
    staging = summarize_stages(
        collect_stages(
            ctx.cfg,
            ctx.file_profile,
            ctx.repo_root,
            ctx.profile,
            include_ownership=False,
        ),
        collect_structured_stages(
            ctx.cfg,
            ctx.file_profile,
            ctx.repo_root,
            ctx.profile,
            include_ownership=False,
        ),
    )
    tree_entries = tuple(_iter_all_trees(ctx))
    codex_configs: tuple[codex_resources_mod.CodexConfigPlan, ...] = ()
    codex_trusted_projects: tuple[Path, ...] = ()
    bootstrap: tuple[Path, ...] = ()
    if ctx.file_selection is None:
        stored_ids = tuple(map(str, reconcile_store.stored_file_ids(ctx.profile)))
        codex_configs = codex_resources_mod.plan_config_resources(
            ctx.cfg,
            ctx.resolved,
            ctx.repo_root,
            read_base=lambda resource_id: reconcile_store.read_base(
                ctx.profile, file_id(resource_id)
            ),
            stored_ids=stored_ids,
            historical_paths=codex_lifecycle.historical_config_paths(),
        )
        codex_trusted_projects = codex_resources_mod.selected_trusted_projects(
            ctx.cfg,
            ctx.resolved,
            ctx.repo_root,
            stored_ids=stored_ids,
            historical_paths=codex_lifecycle.historical_config_paths(),
        )
        bootstrap = tuple(
            Path(str(path)).expanduser() for path in ctx.resolved.bootstrap
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
    tree_held = (
        TreeHoldResolution(section_auto.value) if section_auto is not None else None
    )
    trees = _plan_trees(
        tree_entries, profile=ctx.profile, owner_id=package_owner_id, held=tree_held
    )
    content_paths = tuple(
        deploy.resolve_symlink_target(sub_dst, tf.symlink)
        if tf.symlink is not None
        else sub_dst
        for tf, _, _, sub_dst in tracked_entries
    )
    native_paths = (
        *bootstrap,
        *(codex_plan.destination for codex_plan in codex_configs),
    )
    dst_paths = (*content_paths, *native_paths)
    live_path_snapshot = _snapshot_live_paths(
        {
            *(sub_dst for _, _, _, sub_dst in tracked_entries),
            *content_paths,
            *native_paths,
        }
    )
    file_pre = MappingProxyType(transitions.snapshot_paths(dst_paths))
    file_ownership = _plan_file_ownership(
        tracked_entries,
        profile=ctx.profile,
        owner_id=package_owner_id,
        discard_protected_units=(
            section_auto is reconcile_apply.ReconcileAuto.USE_TRACKED
        ),
    ) + tuple(tree.decision for tree in trees)
    if ctx.file_selection is not None:
        _require_managed_file_selection(file_ownership, package_owner_id)
    _refuse_held_tree_entries(trees)
    preserved_store_ids = _preserved_file_store_ids(
        ctx,
        frozenset(
            [name for _, name, _, _ in tracked_entries]
            + [name for _, name, _, _ in tree_entries]
        ),
    )
    ownership_pre = MappingProxyType(
        transitions.snapshot_paths(
            tuple(
                OwnershipStore().claim_path(decision.observation.resource_id)
                for decision in file_ownership
            )
        )
    )
    deploy.validate_srcs_exist(ctx.cfg, ctx.file_profile, ctx.repo_root)
    if transition:
        transitions.validate_state_dir_writable()
    drift_report = compare_mod.compare_profile(
        ctx.cfg,
        ctx.profile,
        ctx.repo_root,
        ownership_authorized={
            **_file_ownership_authorization(tracked_entries, file_ownership),
            **{
                tree.name: compare_mod.container_authorized(tree.decision)
                for tree in trees
            },
        },
        resolved=ctx.file_profile,
    )
    deploys = _hold_generated_adoptions(
        install_helpers_mod._plan_tracked_files(
            ctx,
            section_auto=section_auto,
            interactive=interactive,
        ),
        file_ownership,
    )
    return InstallPlan(
        ctx=ctx,
        drift_report=drift_report,
        staging=staging,
        deploys=deploys,
        reconcile_store_mutation=install_helpers_mod._planned_reconcile_store_mutation(
            ctx.profile, deploys, preserved_ids=preserved_store_ids
        ),
        bootstrap=bootstrap,
        dst_paths=dst_paths,
        source_bytes=source_bytes,
        tracked_entries=tracked_entries,
        live_paths=live_path_snapshot,
        file_pre=file_pre,
        ownership_pre=ownership_pre,
        file_ownership=file_ownership,
        trees=trees,
        provisioning=ProvisioningPlan(
            cfg_json=ctx.cfg.model_dump_json(), bundles=(), bundle_graphs=(), batches=()
        ),
        package_owner_id=package_owner_id,
        mcp=MCPInstallPlan(value=None),
        extensions=None,
        plugins=None,
        codex_plugins=None,
        codex_configs=codex_configs,
        codex_trusted_projects=codex_trusted_projects,
        preserved_store_ids=preserved_store_ids,
        tree_held=tree_held,
        symlink_conflicts=_symlink_dst_conflicts(tracked_entries),
    )


def _plan_adapters(
    plan: InstallPlan, *, lock: LockFile | None, auto: bool
) -> InstallPlan:
    """Add the extension, plugin, package and MCP decisions of a whole profile."""
    ctx = plan.ctx
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
    codex_plugin_policy = reconcile_adapter.codex_plugin_policy(ctx.resolved)
    if (
        ctx.resolved.codex is not None
        and ctx.cfg.codex is not None
        and (codex_plugin_ids or codex_plugin_policy is ReconcilePolicy.PRUNE)
    ):
        try:
            codex_plugins = codex_plugins_mod.plan_reconcile(
                declared_plugin_ids=codex_plugin_ids,
                marketplaces=ctx.cfg.codex.marketplaces,
                policy=codex_plugin_policy,
            )
        except PluginToolMissing as exc:
            typer.secho(
                f"warning: skipping Codex plugin reconcile — {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )
    return replace(
        plan,
        extensions=extensions,
        plugins=plugins,
        codex_plugins=codex_plugins,
        provisioning=_plan_owned_provisioning(
            ctx, lock=lock, owner_id=plan.package_owner_id
        ),
        mcp=plan_mcp_servers(ctx.cfg, ctx.resolved),
    )


def _hold_generated_adoptions(
    deploys: tuple[_PendingDeploy, ...],
    decisions: tuple[FileDecision, ...],
) -> tuple[_PendingDeploy, ...]:
    """Keep present external generated bytes unchanged during metadata adoption."""
    adopting = {
        decision.observation.locator
        for decision in decisions
        if decision.action in {FileAction.ADOPT, FileAction.TRANSFER}
    }
    held: list[_PendingDeploy] = []
    for record in deploys:
        if record.generated is None:
            held.append(record)
            continue
        content_path = (
            deploy.resolve_symlink_target(record.sub_dst, record.tracked_file.symlink)
            if record.tracked_file.symlink is not None
            else record.sub_dst
        )
        if str(content_path.absolute()) not in adopting or not content_path.is_file():
            held.append(record)
            continue
        live = content_path.read_bytes()
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
        tracked_root=ctx.repo_root / "tracked",
    )


def _plan_file_ownership(
    tracked_entries: tuple[tuple[TrackedFile, str, Path, Path], ...],
    *,
    profile: str,
    owner_id: UUID | None,
    discard_protected_units: bool = False,
) -> tuple[FileDecision, ...]:
    """Freeze container authority independently from unit classifications."""
    store = OwnershipStore()
    planning_owner = owner_id or UUID(int=0)
    return tuple(
        decide_file(
            observation,
            store.read(observation.resource_id),
            owner_id=planning_owner,
            protected_units=(
                not discard_protected_units and _has_protected_units(profile, name)
            ),
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
    held: TreeHoldResolution | None = None,
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
        live = scan_live_tree(destination, policy)
        prior = read_inventory(profile, name)
        tree_plan = plan_tree(desired, live, prior, policy, held)
        observation = observe_tree(destination, live.fingerprint)
        decision = decide_file(
            observation,
            store.read(observation.resource_id),
            owner_id=planning_owner,
            unoccupied=holds_only_state_trees(destination),
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


def _refuse_held_tree_entries(trees: tuple[PlannedTree, ...]) -> None:
    """Refuse before any write, naming every held entry and its resolution."""
    held = [
        f"  {tree.name}: {tree.destination / action.path} ({action.detail})"
        for tree in trees
        for action in tree.plan.actions
        if action.kind is TreeActionKind.HOLD
    ]
    if held:
        raise SetforgeError(
            "managed tree conflicts require review:\n"
            + "\n".join(held)
            + "\nResolve each entry by hand, or rerun with --auto=keep-live to "
            "leave these live entries in place and stop managing them, or "
            "--auto=use-tracked to apply the tracked tree over them. A conflict "
            "involving a directory is never replaced automatically."
        )


def _ownership_destinations(
    tracked: TrackedFile, destination: Path
) -> tuple[Path, ...]:
    """Return every content/topology leaf mutated by one tracked-file entry."""
    if tracked.symlink is None:
        return (destination,)
    target = deploy.resolve_symlink_target(destination, tracked.symlink)
    return tuple(dict.fromkeys((target, destination)))


def _tree_checkpoint_paths(tree: PlannedTree, profile: str) -> tuple[Path, ...]:
    """Return every entry that the frozen tree plan may mutate."""
    # Nothing can exist or be created beneath a live non-directory entry, and
    # a plan never replaces one with a directory.
    beneath_live_leaf = tuple(
        f"{entry.path}/"
        for entry in tree.plan.live.entries
        if entry.kind is not TreeEntryKind.DIRECTORY
    )
    # An inventory written before live scans skipped SetForge's own roots can
    # list them; no plan touches those entries, and journaling them would have
    # recovery restore over the journal itself.
    own = state_trees_under(tree.destination)
    root = tree.destination.absolute()
    prior = tuple(
        entry
        for entry in (tree.plan.prior.entries if tree.plan.prior is not None else ())
        if not any((root / entry.path).is_relative_to(path) for path in own)
    )
    relative = {
        entry.path
        for entries in (
            tree.plan.desired.inventory.entries,
            tree.plan.live.entries,
            prior,
        )
        for entry in entries
        if not entry.path.startswith(beneath_live_leaf)
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
            compare_mod.container_authorized(by_locator[str(path.absolute())])
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
        held=plan.tree_held,
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
        if resolve_generated_file(record.sub_src, spec) != record.generated:
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
    run: _InstallRun,
    *,
    profile: str,
    active_lock: LockFile | None,
    tracked_checkpoint_paths: tuple[Path, ...],
    adapter_kinds: set[operations.AdapterKind],
    retry_failed: bool,
    yes: bool,
    mutation_guards: MutationLockGuards,
) -> None:
    """Apply frozen target plans in the selected bundles' graph order."""
    cfg = plan.ctx.cfg
    retry_failed_ids = (
        _collect_retry_failed_ids(profile) if retry_failed else frozenset()
    )

    def apply_packages() -> CapabilityActivation:
        with (
            run.checkpoint(
                "packages",
                operations.CheckpointKind.IRREVERSIBLE,
                recovery=(
                    "inspect package-manager output and receipts; SetForge will not "
                    "guess an uninstall for potentially user-owned software"
                ),
                paths=plan.package_claim_paths(
                    PackageAction.INSTALL, PackageAction.UPGRADE
                ),
            )
            if _provisioning_plan_has_work(plan.provisioning)
            else nullcontext()
        ):
            provision_results = tuple(reconcile_packages(plan.provisioning))
            run.provision_results = provision_results
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
        status = CapabilityStatus.ACTIVE
        detail = ""
        if plan.provisioning.bundle_graphs:
            if has_hard_failure(provision_results):
                status = CapabilityStatus.FAILED
                detail = "package provisioning reported a hard failure"
            elif any(
                outcome.outcome is Outcome.SOFT
                for result in provision_results
                for outcome in result.outcomes
            ):
                status = CapabilityStatus.SKIPPED
                detail = "package prerequisite was skipped"
        return CapabilityActivation(
            status=status,
            changed=any(not result.delta.is_empty() for result in provision_results),
            detail=detail,
        )

    def apply_files() -> CapabilityActivation:
        with run.checkpoint(
            "tracked-files-and-stores",
            operations.CheckpointKind.REVERSIBLE,
            paths=tracked_checkpoint_paths,
            restore_state=True,
        ):
            codex_resources_mod.assert_projects_trusted(plan.codex_trusted_projects)
            mutation_guards.verify_targets()
            run.deploy_outcome = install_helpers_mod._apply_tracked_file_plan(
                profile,
                plan.deploys,
                preserved_ids=plan.preserved_store_ids,
            )
            _apply_codex_config_plans(
                plan.codex_configs,
                profile=profile,
                mutation_guards=mutation_guards,
            )
            for tree in plan.trees:
                guard = max(
                    (
                        item
                        for item in mutation_guards.targets
                        if tree.destination.absolute().is_relative_to(
                            item.target.absolute()
                        )
                    ),
                    key=lambda item: len(item.target.parts),
                    default=None,
                )
                if guard is None:
                    raise SetforgeError("managed tree target lock is missing")
                guard.verify_expected()
                anchor_fd = guard.target_fd
                if anchor_fd is None:  # pragma: no cover - guard mkdir invariant
                    raise SetforgeError("managed tree target lock has no descriptor")
                if tree.decision.action in {FileAction.ADOPT, FileAction.TRANSFER}:
                    inventory = replace(
                        tree.plan.live,
                        owned_paths=tuple(
                            entry.path for entry in tree.plan.live.entries
                        ),
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
            run.seeded = tuple(
                host_local_record.seed_section_slots_to_store(
                    cfg, plan.ctx.file_profile, plan.ctx.repo_root, profile
                )
            )
            if run.seeded:
                typer.secho(
                    "seeded host-local section template(s): "
                    f"{', '.join(sorted(run.seeded))}",
                    err=True,
                    fg=typer.colors.GREEN,
                )
        return CapabilityActivation(status=CapabilityStatus.ACTIVE, changed=True)

    def apply_extensions() -> CapabilityActivation:
        with run.checkpoint(
            "extensions",
            operations.CheckpointKind.COMPENSATABLE,
            adapters=(operations.AdapterKind.EXTENSIONS,)
            if operations.AdapterKind.EXTENSIONS in adapter_kinds
            else (),
        ):
            run.ext_delta, run.ext_outcomes = _apply_extension_plan(
                plan,
                retry_failed_ids=retry_failed_ids,
                yes=yes,
                lock=active_lock,
            )
        failed = any(
            outcome.status is ReconcileStatus.SKIPPED for outcome in run.ext_outcomes
        )
        return CapabilityActivation(
            status=(
                CapabilityStatus.FAILED
                if failed and bool(plan.provisioning.bundle_graphs)
                else CapabilityStatus.ACTIVE
            ),
            changed=run.ext_delta is not None,
            detail="extension reconciliation left a capability inactive"
            if failed
            else "",
        )

    def apply_plugins() -> CapabilityActivation:
        with run.checkpoint(
            "plugins-and-marketplaces",
            operations.CheckpointKind.COMPENSATABLE,
            adapters=tuple(
                kind
                for kind in (
                    operations.AdapterKind.PLUGINS,
                    operations.AdapterKind.CODEX_PLUGINS,
                )
                if kind in adapter_kinds
            ),
        ):
            run.plugin_delta, run.plugin_outcomes = _apply_plugin_plan(
                plan,
                retry_failed_ids=retry_failed_ids,
                yes=yes,
                lock=active_lock,
            )
            run.codex_plugin_delta, run.codex_plugin_failed = _apply_codex_plugin_plan(
                plan
            )
        failed = bool(run.codex_plugin_failed) or any(
            outcome.status is ReconcileStatus.SKIPPED for outcome in run.plugin_outcomes
        )
        return CapabilityActivation(
            status=(
                CapabilityStatus.FAILED
                if failed and bool(plan.provisioning.bundle_graphs)
                else CapabilityStatus.ACTIVE
            ),
            changed=run.plugin_delta is not None,
            detail="plugin reconciliation left a capability inactive" if failed else "",
        )

    phase_kinds = (
        (CapabilityTargetKind.FILE,)
        if plan.ctx.file_selection is not None
        else (
            CapabilityTargetKind.PACKAGE,
            CapabilityTargetKind.FILE,
            CapabilityTargetKind.EXTENSION,
            CapabilityTargetKind.PLUGIN,
        )
    )
    phase_nodes = tuple(
        CapabilityNode(f"@profile:{kind.value}", kind, ()) for kind in phase_kinds
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
            for kind in phase_kinds
        )
    )
    if not any(
        outcome.target_kind is CapabilityTargetKind.FILE
        and outcome.status is CapabilityStatus.ACTIVE
        for outcome in outcomes
    ):
        initially_absent = {
            snapshot.path
            for snapshot in run.journal.paths
            if snapshot.kind is operations.SnapshotKind.ABSENT
        }
        prepared = tuple(
            guard
            for guard in mutation_guards.targets
            if guard.target.absolute() in initially_absent
        )
        if prepared:
            with run.checkpoint(
                "unused-target-roots",
                operations.CheckpointKind.REVERSIBLE,
                paths=tuple(guard.target for guard in prepared),
            ):
                for guard in prepared:
                    guard.rmdir_if_empty()
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
            CapabilityStatus.RECOVERY_REQUIRED,
        }
    )
    if failed and plan.provisioning.bundle_graphs:
        summary = ", ".join(
            f"{outcome.target_kind.value}={outcome.status.value}" for outcome in failed
        )
        raise SetforgeError(f"capability graph activation failed: {summary}")


def _render_install_plan(
    plan: InstallPlan,
    scan_result: SecretsScanResult,
    *,
    transition: bool = True,
    refusals: tuple[str, ...] = (),
) -> None:
    """Render the same immutable plan the real install path consumes."""
    _dry_run_pipeline(
        ctx=plan.ctx,
        drift_report=plan.drift_report,
        staging=plan.staging,
        deploys=plan.deploys,
        provisioning=plan.provisioning,
        mcp=plan.mcp,
        extensions=plan.extensions,
        plugins=plan.plugins,
        immutable_plan=True,
        secrets_scan=scan_result,
        record_transition=transition and not _install_plan_recorded_nothing(plan),
        refusals=refusals,
    )
    changed_codex = [codex for codex in plan.codex_configs if codex.changed]
    if changed_codex:
        typer.echo("=== would-be Codex config changes ===")
        for codex in changed_codex:
            verb = "create" if codex.live is None else "update"
            typer.echo(f"  WOULD {verb}  {codex.destination}")
            diff = difflib.unified_diff(
                (codex.live or b"").decode("utf-8", "replace").splitlines(),
                codex.result.decode("utf-8", "replace").splitlines(),
                lineterm="",
                n=0,
            )
            for line in list(diff)[2:]:
                typer.echo(f"    {line}")
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


def _render_preinstall_staging(
    staging: tuple[StageSummary, ...],
) -> None:
    actionable = tuple(row for row in staging if row.pending or row.reconfirm_required)
    if not actionable:
        return
    typer.echo("=== pre-install staging classifications ===")
    for row in actionable:
        typer.echo(
            f"{row.name}: {row.shared_promotable} shared-promotable  "
            f"{row.drafted} drafted  {row.reconfirm_required} "
            f"reconfirm-required  {row.local} local  {row.pending} pending"
        )
        for blocker in row.blockers:
            if "run `setforge stage" in blocker:
                typer.echo(f"  kept host-only: {blocker}")


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
    active_lock = parse_lock(path.read_bytes()) if path.exists() else None
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
    decisions: tuple[PackageDecision, ...], *, yes: bool, receiver_owner: UUID | None
) -> None:
    """Confirm metadata-only ownership claims before the first effect."""
    decisions = tuple(
        decision
        for decision in decisions
        if decision.action in {PackageAction.ADOPT, PackageAction.TRANSFER}
    )
    if not decisions:
        return
    transfers = tuple(
        decision for decision in decisions if decision.action is PackageAction.TRANSFER
    )
    if transfers:
        if receiver_owner is None:
            raise SetforgeError(
                "package ownership transfer requires a Git-backed config"
            )
        for decision in transfers:
            assert decision.claim is not None
            typer.echo(
                "package ownership transfer: "
                f"{decision.resource_id.canonical()} "
                f"{decision.claim.owner_id} -> {receiver_owner}"
            )
    if yes:
        return
    names = ", ".join(decision.item.identity.display for decision in decisions)
    if not sys.stdin.isatty():
        raise SetforgeError(
            f"package ownership change requires confirmation for {names}; "
            "rerun with --yes"
        )
    if not typer.confirm(
        f"Manage or transfer existing package(s) without reinstalling: {names}?",
        default=False,
    ):
        raise SetforgeError(
            "package ownership change declined; no package changes applied"
        )


def _blocked_install_message(
    decision: FileDecision, *, owner_id: UUID | None, config: Path
) -> str:
    path = decision.observation.locator
    message = f"tracked file ownership blocks install for {path}: {decision.detail}"
    claim = decision.claim
    if not decision.observation.present and claim is not None:
        message += (
            "; restore the file by hand, or run "
            "`setforge install --auto=use-tracked --yes` to recreate it from the "
            "tracked version and discard those units"
        )
    elif (
        claim is not None
        and owner_id is not None
        and claim.owner_id == owner_id
        and claim.lifecycle is ClaimLifecycle.RELEASED
    ):
        message += _released_claim_remedy(claim, owner_id, config)
    return message


def _released_claim_remedy(claim: OwnershipClaim, owner_id: UUID, config: Path) -> str:
    claim_id = OwnershipStore().claim_id(claim.resource_id)
    release = next(
        (
            transition
            for transition in reversed(OwnershipHistoryStore().list(owner_id))
            if transition.after.resource_id == claim.resource_id
            and transition.after.generation == claim.generation
        ),
        None,
    )
    if release is None:
        return (
            f" (claim {claim_id} was released; find the release with "
            f"`setforge ownership history --config={config}` and undo it with "
            "`setforge ownership revert <transition-id> --yes`)"
        )
    return (
        f" (claim {claim_id} was released; take ownership back with "
        f"`setforge ownership revert {release.transition_id} "
        f"--config={config} --yes`)"
    )


def _confirm_file_adoptions(
    decisions: tuple[FileDecision, ...],
    *,
    yes: bool,
    receiver_owner: UUID | None,
    config: Path,
) -> None:
    """Confirm container claims separately from reconcile content choices."""
    adopt = tuple(
        decision
        for decision in decisions
        if decision.action in {FileAction.ADOPT, FileAction.TRANSFER}
    )
    blocked = tuple(
        decision for decision in decisions if decision.action is FileAction.HOLD
    )
    if blocked:
        raise SetforgeError(
            "\n".join(
                _blocked_install_message(
                    decision, owner_id=receiver_owner, config=config
                )
                for decision in blocked
            )
        )
    if not adopt:
        return
    for decision in adopt:
        if decision.action is not FileAction.TRANSFER:
            continue
        if decision.claim is None or receiver_owner is None:
            raise SetforgeError("file ownership transfer requires a Git-backed config")
        typer.echo(
            "file ownership transfer: "
            f"{decision.observation.resource_id.canonical()} "
            f"{decision.claim.owner_id} -> {receiver_owner} "
            f"({decision.observation.locator})"
        )
    if yes:
        return
    names = ", ".join(decision.observation.locator for decision in adopt)
    operation = (
        "file ownership transfer"
        if any(decision.action is FileAction.TRANSFER for decision in adopt)
        else "file adoption"
    )
    if not sys.stdin.isatty():
        raise SetforgeError(
            f"{operation} requires confirmation for {names}; rerun with --yes"
        )
    if not typer.confirm(
        f"Manage or transfer tracked file(s) without changing their bytes: {names}?",
        default=False,
    ):
        raise SetforgeError("file ownership change declined; no file changes applied")


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
        return read_owner_id(repo_root)
    except OwnershipError:
        try:
            resolve_owner_common_dir(repo_root)
        except OwnershipError:
            return None
        return uuid4()


def _preview_file_ownership(
    config: Path,
    profile: str,
    *,
    owner_id_override: UUID | None = None,
    file_selection: frozenset[str] | None = None,
    discard_protected_units: bool = False,
) -> tuple[FileDecision, ...]:
    """Build the file consent surface without holding mutation locks."""
    cfg = load_config(config)
    ctx, _overlay = _resolve_install_profile(
        cfg, profile, config.parent, file_selection
    )
    owner_id = owner_id_override
    if owner_id is None:
        try:
            owner_id = read_owner_id(config.parent)
        except OwnershipError:
            owner_id = None
    regular = _plan_file_ownership(
        tuple(_iter_all_tracked_files(ctx)),
        profile=profile,
        owner_id=owner_id,
        discard_protected_units=discard_protected_units,
    )
    trees = _plan_trees(tuple(_iter_all_trees(ctx)), profile=profile, owner_id=owner_id)
    return regular + tuple(tree.decision for tree in trees)


def _preview_file_declaration_refs(
    config: Path, profile: str
) -> dict[ResourceId, tuple[str, ...]]:
    """Resolve exact current declaration refs for every file-container resource."""
    cfg = load_config(config)
    resolved = resolve_effective_profile(cfg, profile, config.parent).resolved
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=config.parent, profile=profile
    )
    refs: dict[ResourceId, list[str]] = {}
    for tracked, name, _source, destination in _iter_all_tracked_files(ctx):
        for owned_destination in _ownership_destinations(tracked, destination):
            resource_id = file_resource_id(owned_destination)
            refs.setdefault(resource_id, []).append(f"tracked_files.{name}")
    for _tracked, name, _source, destination in _iter_all_trees(ctx):
        resource_id = file_resource_id(destination)
        refs.setdefault(resource_id, []).append(f"tracked_files.{name}")
    return {
        resource_id: tuple(sorted(set(declaration_refs)))
        for resource_id, declaration_refs in refs.items()
    }


def _preview_tree_targets(
    config: Path, profile: str, *, file_selection: frozenset[str] | None = None
) -> tuple[Path, ...]:
    """Resolve explicit filesystem roots for mutation-lock acquisition."""
    cfg = load_config(config)
    ctx, _overlay = _resolve_install_profile(
        cfg, profile, config.parent, file_selection
    )
    roots = {
        *(
            _tree_lock_target(destination)
            for *_prefix, destination in _iter_all_trees(ctx)
        ),
        *(
            ()
            if file_selection is not None
            else codex_resources_mod.config_target_roots(
                cfg,
                ctx.resolved,
                config.parent,
                stored_ids=tuple(map(str, reconcile_store.stored_file_ids(profile))),
                historical_paths=codex_lifecycle.historical_config_paths(),
            )
        ),
    }
    return tuple(sorted(roots, key=str))


def _read_package_owner_id(repo_root: Path) -> UUID | None:
    """Read an established owner without making dry-run metadata changes."""
    try:
        return read_owner_id(repo_root)
    except OwnershipError:
        return None


def _require_managed_file_selection(
    decisions: tuple[FileDecision, ...], owner_id: UUID | None
) -> None:
    if owner_id is None:
        raise SetforgeError(
            "file-only install requires this checkout's existing ownership"
        )
    refused = [
        decision.observation.locator
        for decision in decisions
        if decision.claim is None
        or decision.claim.owner_id != owner_id
        or decision.claim.authority is not Authority.MANAGE
        or decision.claim.lifecycle is not ClaimLifecycle.CLAIMED
        or decision.action is FileAction.HOLD
    ]
    if refused:
        raise SetforgeError(
            "file-only install requires already-managed files from this checkout: "
            + ", ".join(refused)
        )


def _publish_package_adoptions(
    plan: InstallPlan,
) -> tuple[transitions.OwnershipTransferDelta, ...]:
    """Publish confirmed claims after exact locked plan revalidation."""
    decisions = tuple(
        decision
        for decision in plan.provisioning.ownership
        if decision.action in {PackageAction.ADOPT, PackageAction.TRANSFER}
    )
    if not decisions:
        return ()
    owner_id = plan.package_owner_id
    if owner_id is None:
        typer.secho(
            "warning: existing packages remain unowned because the configuration "
            "is not Git-backed",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return ()
    store = OwnershipStore()
    receipts = ReceiptStore(default_receipt_root())
    transfers: list[transitions.OwnershipTransferDelta] = []
    for decision in decisions:
        if store.read(decision.resource_id) != decision.claim:
            raise SetforgeError("package ownership changed after confirmation; retry")
        if decision.action is PackageAction.TRANSFER:
            if decision.claim is None:
                raise SetforgeError("package transfer lost its current claim")
            after = store.transfer_locked(
                decision.resource_id,
                expected_owner=decision.claim.owner_id,
                new_owner=owner_id,
                expected_generation=decision.claim.generation,
                declaration_refs=(
                    f"packages.{decision.item.type}.{decision.item.identity.key}",
                ),
            )
            transfers.append(transitions.OwnershipTransferDelta(decision.claim, after))
            typer.echo(
                f"transferred package ownership: {decision.item.identity.display} "
                "(no package bytes changed)"
            )
            continue
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
    return tuple(transfers)


def _publish_adoptions_checkpoint(
    plan: InstallPlan, run: _InstallRun
) -> tuple[transitions.OwnershipTransferDelta, ...]:
    """Journal and publish metadata-only adoption claims."""
    claim_paths = plan.package_claim_paths(PackageAction.ADOPT, PackageAction.TRANSFER)
    if not claim_paths:
        return ()
    with run.checkpoint(
        "package-adoption",
        operations.CheckpointKind.REVERSIBLE,
        paths=(*claim_paths, *_legacy_adoption_receipt_paths(plan)),
    ):
        return _publish_package_adoptions(plan)


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
            live = scan_live_tree(tree.destination, policy)
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
        if not observed.present or (
            current is not None and current.fingerprint == observed.fingerprint
        ):
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
            deploy.resolve_symlink_target(record.sub_dst, record.tracked_file.symlink)
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
    plan: InstallPlan, run: _InstallRun
) -> tuple[transitions.OwnershipTransferDelta, ...]:
    claim_paths = plan.file_claim_paths(FileAction.ADOPT, FileAction.TRANSFER)
    if not claim_paths:
        return ()
    transfers: list[transitions.OwnershipTransferDelta] = []
    with run.checkpoint(
        "file-adoption", operations.CheckpointKind.REVERSIBLE, paths=claim_paths
    ):
        store = OwnershipStore()
        declarations = {
            str(path.absolute()): name
            for tracked, name, _source, destination in plan.tracked_entries
            for path in _ownership_destinations(tracked, destination)
        }
        declarations.update(
            {str(tree.destination.absolute()): tree.name for tree in plan.trees}
        )
        trees_by_locator = {
            str(tree.destination.absolute()): tree for tree in plan.trees
        }
        for decision in plan.file_ownership:
            if decision.action is not FileAction.TRANSFER:
                continue
            before = decision.claim
            if before is None or store.read(decision.observation.resource_id) != before:
                raise SetforgeError(
                    "tracked file ownership changed after confirmation; retry"
                )
            tree = trees_by_locator.get(decision.observation.locator)
            if tree is None:
                observed = observe_file(
                    Path(decision.observation.locator),
                    allow_topology=decision.observation.topology,
                )
            else:
                assert tree.tracked_file.tree is not None
                live_inventory = scan_live_tree(
                    tree.destination, tree.tracked_file.tree
                )
                observed = observe_tree(tree.destination, live_inventory.fingerprint)
            if observed != decision.observation:
                raise SetforgeError(
                    "tracked file changed after transfer confirmation; retry"
                )
            owner_id = plan.package_owner_id
            if owner_id is None:
                raise SetforgeError("ownership transfer requires a Git-backed config")
            after = store.transfer_locked(
                before.resource_id,
                expected_owner=before.owner_id,
                new_owner=owner_id,
                expected_generation=before.generation,
                declaration_refs=(
                    f"tracked_files.{declarations[decision.observation.locator]}",
                ),
            )
            transfers.append(transitions.OwnershipTransferDelta(before, after))
            typer.echo(
                f"transferred tracked file ownership: {decision.observation.locator} "
                "(no file bytes changed)"
            )
        _publish_file_claims(plan, actions=frozenset({FileAction.ADOPT}))
    return tuple(transfers)


def _refresh_file_claims_checkpoint(plan: InstallPlan, run: _InstallRun) -> None:
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
        return
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
    run.journal = operations.extend_paths(run.journal, paths)
    with run.checkpoint(
        "file-ownership-refresh", operations.CheckpointKind.REVERSIBLE, paths=paths
    ):
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
    config: Path,
    profile: str,
    *,
    locked: bool,
    owner_id_override: UUID | None = None,
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
    owner_id = owner_id_override
    if owner_id is None:
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
        tracked_root=config.parent / "tracked",
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


def _confirm_install(
    plan: InstallPlan,
    *,
    config: Path,
    local_overlay: LocalOverlayResolution,
    ownership_preview: tuple[PackageDecision, ...],
    file_ownership_preview: tuple[FileDecision, ...],
    section_auto: reconcile_apply.ReconcileAuto | None,
    fresh: bool,
    auto: str | None,
    yes: bool,
    no_secrets_scan: bool,
    no_transition: bool,
) -> SecretPlan | None:
    """Ask every under-lock question; ``None`` means the welcome was declined."""
    ctx = plan.ctx
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
            run_dry_run=lambda: _render_install_plan(
                plan,
                scan_result,
                transition=not no_transition,
                refusals=_plan_refusals(plan, yes=yes),
            ),
        )
        if welcome_choice is not WelcomeChoice.PROCEED:
            return None

    _render_preinstall_staging(plan.staging)
    _run_predeploy_gates(
        drift_report=plan.drift_report,
        ctx=ctx,
        yes=yes,
    )
    install_helpers_mod._confirm_use_tracked_or_exit(
        deploys=plan.deploys,
        profile=ctx.profile,
        section_auto=section_auto,
        yes=yes,
    )
    if plan.provisioning.ownership != ownership_preview:
        raise SetforgeError(
            "package ownership inputs changed after confirmation; retry"
        )
    if plan.file_ownership != file_ownership_preview:
        confirmation_actions = {
            FileAction.ADOPT,
            FileAction.TRANSFER,
            FileAction.HOLD,
        }
        if any(
            decision.action in confirmation_actions
            for decision in (*file_ownership_preview, *plan.file_ownership)
        ):
            raise SetforgeError(
                "file ownership inputs changed after confirmation; retry"
            )

    # Refuse-before-write: deploy_symlinked_file() raises on an occupied
    # symlink dst only at write time, after earlier files have landed.
    if plan.symlink_conflicts:
        raise SetforgeError("\n".join(plan.symlink_conflicts))

    secret_plan = _plan_secret_findings(scan_result, yes=yes)
    if secret_plan is None:
        typer.secho("install aborted by secrets scan", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return secret_plan


@app.command(epilog=INSTALL_EXAMPLES)
def install(  # noqa: C901 - confirmation and frozen-plan orchestration
    profile: str = _PROFILE_OPTION,
    config: Path | None = _CONFIG_OPTION,
    file: list[str] | None = typer.Option(
        None,
        "--file",
        help="Update only this declared tracked-file ID in the profile. Repeat to "
        "select several already-managed files; other resources are retained.",
    ),
    no_transition: bool = typer.Option(
        False,
        "--no-transition",
        hidden=True,
        help="Skip writing a transition record (testing / debugging).",
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
            "Held managed-tree entries follow the same choice: 'use-tracked' "
            "applies the tracked tree over them, 'keep-live' leaves them in "
            "place and stops managing them. "
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
    """Deploy tracked → live for the profile or selected managed files."""
    file_selection = None if file is None else frozenset(file)
    if file_selection is not None and retry_failed:
        raise SetforgeError("--file and --retry-failed cannot be combined")
    # Canonicalize once so a symlink retarget cannot split source discovery,
    # locking, config loading, and input snapshots across two repositories.
    config_is_explicit = config is not None
    config = _resolve_config_arg(config).resolve()
    # Mutual-exclusivity guard for the new section-reconcile flags.
    section_auto = _parse_section_auto(auto, reconcile_user_sections)

    repo_root = config.parent
    install_source = (
        source_mod.PathSource(path=repo_root)
        if config_is_explicit
        else resolve_source_for_git_check(repo_root)
    )
    # Source acquisition precedes config loading so this invocation plans the
    # checkout it just fetched, rather than a stale in-memory model.
    # Dry-run builds the same plan as apply and renders it without entering the
    # mutation phase. The flag stays at this orchestration boundary.
    if dry_run:
        _fetch_upstream(install_source, no_fetch=no_fetch, dry_run=True)
        run_git_check_or_raise(source=install_source, no_git_check=no_git_check)
        plan, _active_lock, _local_overlay = _plan(
            config,
            profile,
            repo_root,
            locked=locked,
            file_selection=file_selection,
            section_auto=section_auto,
            interactive=False,
            transition=not no_transition,
            auto=True,
            package_owner_id=_read_package_owner_id(repo_root),
        )
        scan_result = secrets_mod.run_pre_deploy_scan(
            tracked_root=config.parent / "tracked",
            skip=no_secrets_scan,
        )
        _render_install_plan(
            plan,
            scan_result,
            transition=not no_transition,
            refusals=_plan_refusals(plan, yes=yes),
        )
        return

    with mutation_locks(resources=True):
        _fetch_upstream(install_source, no_fetch=no_fetch, dry_run=False)
        run_git_check_or_raise(source=install_source, no_git_check=no_git_check)
    # The run creates the state root after planning; a managed tree holding it
    # or its absent parent would otherwise scan differently at confirmation.
    transitions.state_root().mkdir(parents=True, exist_ok=True)
    ownership_config = config.read_bytes()
    ownership_preview = (
        _preview_package_ownership(config, profile, locked=locked)
        if file_selection is None
        else ()
    )
    file_ownership_preview = _preview_file_ownership(
        config,
        profile,
        file_selection=file_selection,
        discard_protected_units=(
            section_auto is reconcile_apply.ReconcileAuto.USE_TRACKED
        ),
    )
    tree_target_preview = _preview_tree_targets(
        config, profile, file_selection=file_selection
    )
    if config.read_bytes() != ownership_config:
        raise SetforgeError("install configuration changed while loading; retry")
    if file_selection is not None:
        package_owner_id = _read_package_owner_id(repo_root)
        _require_managed_file_selection(file_ownership_preview, package_owner_id)
    else:
        package_owner_id = _prepare_package_owner_id(
            repo_root,
            ownership_preview,
            required=bool(file_ownership_preview),
        )
    _confirm_package_adoptions(
        ownership_preview, yes=yes, receiver_owner=package_owner_id
    )
    if (repo_root / ".git").exists():
        _confirm_file_adoptions(
            file_ownership_preview,
            yes=yes,
            receiver_owner=package_owner_id,
            config=config,
        )
    has_transfer = any(
        decision.action is PackageAction.TRANSFER for decision in ownership_preview
    ) or any(
        decision.action is FileAction.TRANSFER for decision in file_ownership_preview
    )
    if has_transfer and package_owner_id is None:
        raise SetforgeError("ownership transfer requires a Git-backed config")
    if has_transfer and no_transition:
        raise SetforgeError(
            "ownership transfer requires transition recording; remove --no-transition"
        )

    with operations.transaction(
        resources=True,
        config_identity_dir=(
            resolve_owner_common_dir(repo_root)
            if package_owner_id is not None
            else None
        ),
        config_dir=config.parent,
        target_roots=tree_target_preview,
        profile=profile,
        recover=(profile, "install"),
    ) as mutation_guards:
        if config.read_bytes() != ownership_config:
            raise SetforgeError(
                "install configuration changed after confirmation; retry"
            )
        identity_guard = (
            mutation_guards.config_identity if mutation_guards is not None else None
        )
        fresh = is_fresh_host()
        interactive = _want_interactive_reconcile(
            reconcile_user_sections=reconcile_user_sections,
            section_auto=section_auto,
        )
        plan, active_lock, local_overlay = _plan(
            config,
            profile,
            repo_root,
            locked=locked,
            file_selection=file_selection,
            section_auto=section_auto,
            interactive=interactive,
            transition=not no_transition,
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
        secret_plan = _confirm_install(
            plan,
            config=config,
            local_overlay=local_overlay,
            ownership_preview=ownership_preview,
            file_ownership_preview=file_ownership_preview,
            section_auto=section_auto,
            fresh=fresh,
            auto=auto,
            yes=yes,
            no_secrets_scan=no_secrets_scan,
            no_transition=no_transition,
        )
        if secret_plan is None:
            return

        # This is the first mutation boundary. Every refusal and confirmation
        # above has completed, and apply consumes the frozen plan below.
        _assert_plan_inputs_unchanged(plan)
        _validate_external_plan(plan)
        if not no_transition:
            transitions.ensure_state_dir_writable()

        deploy_state_pre = install_helpers_mod._capture_store_snapshots(
            profile, plan.deploys, preserved_ids=plan.preserved_store_ids
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
        tree_paths = plan.tree_paths()
        write_targets = _symlink_write_targets(plan.dst_paths)
        tracked_paths = (
            *plan.dst_paths,
            *_journalable_backups(plan.dst_paths),
            *write_targets,
            *_journalable_backups(write_targets),
            *(sub_dst for _, _, _, sub_dst in plan.tracked_entries),
            *tree_paths,
        )
        journal_paths = tuple(
            dict.fromkeys(
                (
                    *tracked_paths,
                    *secrets_checkpoint_paths,
                    *plan.package_claim_paths(
                        PackageAction.ADOPT,
                        PackageAction.TRANSFER,
                        PackageAction.INSTALL,
                        PackageAction.UPGRADE,
                    ),
                    *plan.file_claim_paths(
                        FileAction.ADOPT,
                        FileAction.TRANSFER,
                        FileAction.INSTALL,
                        FileAction.MANAGE,
                        FileAction.REVIEW,
                    ),
                    *_legacy_adoption_receipt_paths(plan),
                )
            )
        )
        missing_roots = tuple(
            guard.target for guard in mutation_guards.targets if guard.target_fd is None
        )
        alias_paths, install_parent_guards = operations.capture_install_parent_guards(
            journal_paths, missing_roots
        )
        journal_paths = tuple(dict.fromkeys((*journal_paths, *alias_paths)))
        tracked_checkpoint_paths = tuple(
            dict.fromkeys(
                (
                    *tracked_paths,
                    *plan.file_claim_paths(
                        FileAction.INSTALL, FileAction.MANAGE, FileAction.REVIEW
                    ),
                )
            )
        )
        symlink_paths = frozenset(
            destination
            for tracked, _name, _source, destination in plan.tracked_entries
            if tracked.symlink is not None
        )
        tree_filesystem_paths = tuple(
            dict.fromkeys((*tree_paths, *sorted(symlink_paths, key=str)))
        )
        tree_pre_images = {
            path: transitions.snapshot_filesystem_image(path)
            for path in tree_filesystem_paths
        }
        file_pre_images = transitions.capture_files(
            (*plan.dst_paths, *plan.ownership_pre)
        )
        adapter_snapshots = _install_adapter_snapshots(plan)
        adapter_kinds = {item.kind for item in adapter_snapshots}
        if package_owner_id is not None:
            if identity_guard is None:
                raise SetforgeError("config owner identity lock was not acquired")
            if (
                load_or_create_owner_id_locked(
                    repo_root, identity_guard.directory_fd, package_owner_id
                )
                != package_owner_id
            ):
                raise SetforgeError(
                    "config owner identity changed after confirmation; retry"
                )
        run = _InstallRun(
            operations.prepare(
                command="install",
                profile=profile,
                config_dir=config.parent,
                resources_lock=True,
                paths=journal_paths,
                path_guards=install_parent_guards,
                state_snapshots=state_pre,
                adapters=adapter_snapshots,
            )
        )
        applied = _apply_install(
            plan,
            run,
            profile=profile,
            secret_plan=secret_plan,
            secrets_checkpoint_paths=secrets_checkpoint_paths,
            file_pre_images=file_pre_images,
            active_lock=active_lock,
            tracked_checkpoint_paths=tracked_checkpoint_paths,
            adapter_kinds=adapter_kinds,
            retry_failed=retry_failed,
            yes=yes,
            mutation_guards=mutation_guards,
        )
        _record_install(
            plan,
            run,
            profile=profile,
            no_transition=no_transition,
            ownership_transfers=applied.ownership_transfers,
            file_pre=applied.file_pre,
            files_applied=applied.files_applied,
            deploy_outcome=applied.deploy_outcome,
            mcp_delta=applied.mcp_delta,
            state_pre=state_pre,
            tree_filesystem_paths=tree_filesystem_paths,
            tree_pre_images=tree_pre_images,
            symlink_paths=symlink_paths,
        )
        operations.complete(run.journal)

        _gate_on_mcp_failures(applied.mcp_failed)
        if run.codex_plugin_failed:
            details = "; ".join(
                f"{item}: {error}" for item, error in run.codex_plugin_failed
            )
            raise SetforgeError(f"Codex plugin reconciliation failed: {details}")
        _gate_on_provisioning_failures(list(run.provision_results))
        _gate_on_deferred_reconcile(
            applied.deploy_outcome.deferred_reconcile, interactive
        )


class _AppliedInstall(NamedTuple):
    """What the apply phase hands to the transition record and exit gates."""

    ownership_transfers: tuple[transitions.OwnershipTransferDelta, ...]
    file_pre: dict[Path, transitions.FilesystemImage]
    files_applied: bool
    deploy_outcome: install_helpers_mod.DeployOutcome
    mcp_delta: transitions.MCPDelta | None
    mcp_failed: list[tuple[str, str]]


def _apply_install(
    plan: InstallPlan,
    run: _InstallRun,
    *,
    profile: str,
    secret_plan: SecretPlan,
    secrets_checkpoint_paths: tuple[Path, ...],
    file_pre_images: dict[Path, transitions.FilesystemImage],
    active_lock: LockFile | None,
    tracked_checkpoint_paths: tuple[Path, ...],
    adapter_kinds: set[operations.AdapterKind],
    retry_failed: bool,
    yes: bool,
    mutation_guards: MutationLockGuards,
) -> _AppliedInstall:
    """Apply the frozen plan, one journaled checkpoint per phase."""
    cfg = plan.ctx.cfg
    resolved = plan.ctx.resolved
    _apply_secrets_and_bootstrap(
        run,
        secret_plan=secret_plan,
        bootstrap=plan.bootstrap,
        checkpoint_paths=secrets_checkpoint_paths,
        target_guards=mutation_guards.targets,
        codex_roots=frozenset(
            item.destination.parent.absolute() for item in plan.codex_configs
        ),
    )
    ownership_transfers = (
        *_publish_adoptions_checkpoint(plan, run),
        *_publish_file_adoptions_checkpoint(plan, run),
    )

    # For symlink-deployed tracked_files the recorded file is the
    # symlink's TARGET (where bytes actually land); the link itself is a
    # tree filesystem delta. Store files (byte bases, spans sidecars,
    # scalar-base manifests) are not recorded here: their pre-install
    # state is captured at the pass-2 barrier (state_snapshots below) and
    # revert restores them through that mechanism. Transfer claims have an
    # exact, generation-checked sidecar inverse, so recording those claim
    # files too would reverse them twice.
    transfer_claim_paths = {
        OwnershipStore().claim_path(transfer.after.resource_id)
        for transfer in ownership_transfers
    }
    file_pre = {
        path: image
        for path, image in file_pre_images.items()
        if path not in transfer_claim_paths
    }

    _apply_capability_targets(
        plan,
        run,
        profile=profile,
        active_lock=active_lock,
        tracked_checkpoint_paths=tracked_checkpoint_paths,
        adapter_kinds=adapter_kinds,
        retry_failed=retry_failed,
        yes=yes,
        mutation_guards=mutation_guards,
    )
    files_applied = run.deploy_outcome is not None
    if files_applied:
        _refresh_file_claims_checkpoint(plan, run)
    deploy_outcome = run.deploy_outcome or install_helpers_mod.DeployOutcome()
    with run.checkpoint(
        "mcp-servers",
        operations.CheckpointKind.COMPENSATABLE,
        adapters=(operations.AdapterKind.MCP,)
        if operations.AdapterKind.MCP in adapter_kinds
        else (),
    ):
        mcp_delta, mcp_failed = reconcile_mcp_servers(cfg, resolved, plan=plan.mcp)
    return _AppliedInstall(
        ownership_transfers,
        file_pre,
        files_applied,
        deploy_outcome,
        mcp_delta,
        mcp_failed,
    )


def _record_install(
    plan: InstallPlan,
    run: _InstallRun,
    *,
    profile: str,
    no_transition: bool,
    ownership_transfers: tuple[transitions.OwnershipTransferDelta, ...],
    file_pre: dict[Path, transitions.FilesystemImage],
    files_applied: bool,
    deploy_outcome: install_helpers_mod.DeployOutcome,
    mcp_delta: transitions.MCPDelta | None,
    state_pre: tuple[transitions.StateSnapshotEntry, ...],
    tree_filesystem_paths: tuple[Path, ...],
    tree_pre_images: dict[Path, transitions.FilesystemImage],
    symlink_paths: frozenset[Path],
) -> None:
    """Write the transition record of what the applied install changed."""
    ctx = plan.ctx
    file_post = transitions.capture_files(file_pre)
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
        if tree_pre_images[path] != tree_post_images[path] or path in symlink_paths
    )

    _emit_reconcile_summary(run.plugin_outcomes, run.ext_outcomes)

    if not no_transition and not _install_recorded_nothing(
        file_pre=file_pre,
        file_post=file_post,
        deploy_outcome=deploy_outcome,
        ext_delta=run.ext_delta,
        plugin_delta=run.plugin_delta,
        codex_plugin_delta=run.codex_plugin_delta,
        mcp_delta=mcp_delta,
        reconcile_outcomes=run.plugin_outcomes + run.ext_outcomes,
        seeded=bool(run.seeded),
        codex_base_mutated=any(
            entry.store is transitions.SnapshotStore.BASE
            and entry.key.startswith(("codex/config/", "codex/mcp-target/"))
            and transitions.snapshot_store_state(entry.store, entry.profile, entry.key)
            != entry
            for entry in state_pre
        ),
        filesystem_deltas=tree_filesystem_deltas,
        ownership_transfers=ownership_transfers,
    ):
        with run.checkpoint(
            "transition-record",
            operations.CheckpointKind.REVERSIBLE,
            restore_transitions=True,
        ):
            tracked_file_destinations = {}
            if files_applied:
                tracked_file_destinations = {
                    name: _ownership_destinations(tracked, destination)
                    for tracked, name, _source, destination in plan.tracked_entries
                }
                tracked_file_destinations.update(
                    {
                        tree.name: (
                            tree.destination,
                            *(
                                tree.destination / entry.path
                                for entry in tree.plan.desired.inventory.entries
                            ),
                        )
                        for tree in plan.trees
                    }
                )
            target = _write_install_transition(
                profile,
                file_pre,
                file_post,
                run.ext_delta,
                run.plugin_delta,
                codex_plugin_delta=run.codex_plugin_delta,
                source_dir=ctx.repo_root,
                reconcile_outcomes=run.plugin_outcomes + run.ext_outcomes,
                state_snapshots=state_pre,
                mcp_delta=mcp_delta,
                filesystem_deltas=tree_filesystem_deltas,
                ownership_transfers=ownership_transfers,
                tracked_file_destinations=tracked_file_destinations,
            )
            typer.echo(f"transition: {target}")
            typer.echo(f"↩  revert with: setforge revert --profile={profile}")


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
                            "planned": [
                                [list(entry.command), entry.scope]
                                for entry in plan.mcp.value.entries
                                if entry.name == name
                            ],
                            "context": plan.mcp.value.context,
                        }
                        for name, prior in plan.mcp.value.preconditions
                    ],
                    sort_keys=True,
                ),
            )
        )
    return tuple(snapshots)


def _symlink_dst_conflicts(
    tracked_entries: tuple[tuple[TrackedFile, str, Path, Path], ...],
) -> tuple[str, ...]:
    """Name every symlink tracked_file whose dst is already occupied.

    Mirrors the refusal in :func:`deploy.deploy_symlinked_file` (a regular file
    or a directory — but NOT a pre-existing symlink — sitting at the link's
    ``dst``). The planner records it so the preview reports the refusal and
    apply raises it before any file is written or any store is mutated: at
    write time a symlink ordered after regular-file tracked_files would let
    those earlier writes land and then raise with no transition recorded. Every
    conflicting dst is named so the user sees the complete set at once.
    """
    return tuple(
        f"refusing to deploy symlink at {sub_dst}: a "
        f"{'directory' if sub_dst.is_dir() else 'regular file'} is already "
        f"present. Move it aside or remove it before deploying "
        f"tracked_file with symlink: {tracked_file.symlink!r}."
        for tracked_file, _sub_name, _sub_src, sub_dst in tracked_entries
        if tracked_file.symlink is not None
        and not sub_dst.is_symlink()
        and sub_dst.exists()
    )


def _plan_refusals(plan: InstallPlan, *, yes: bool) -> tuple[str, ...]:
    """Return what apply refuses for this plan, in the order apply checks it."""
    drift = install_helpers_mod._unexpected_drift_refusal(
        plan.drift_report, plan.ctx, yes=yes
    )
    return (*(() if drift is None else (drift,)), *plan.symlink_conflicts)


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
    target = allowlist_path or secrets_mod.default_allowlist_path()
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
    run: _InstallRun,
    *,
    secret_plan: SecretPlan,
    bootstrap: tuple[Path, ...],
    checkpoint_paths: tuple[Path, ...],
    target_guards: tuple[TargetLockGuard, ...],
    codex_roots: frozenset[Path],
) -> None:
    """Prepare guarded roots and apply the first reversible phase."""
    missing_guards = tuple(guard for guard in target_guards if guard.target_fd is None)
    if {guard.target.absolute() for guard in missing_guards}.intersection(
        path.absolute() for path in bootstrap
    ):
        raise SetforgeError("bootstrap file conflicts with a managed directory root")
    if missing_guards:
        with run.checkpoint(
            "prepare-target-roots",
            operations.CheckpointKind.REVERSIBLE,
            paths=tuple(guard.target for guard in missing_guards),
        ):
            roots: list[tuple[Path, int, int, int]] = []
            for guard in missing_guards:
                guard.verify_expected()
                guard.mkdir(
                    mode=0o700 if guard.target.absolute() in codex_roots else 0o777
                )
                assert guard.target_fd is not None
                info = os.fstat(guard.target_fd)
                roots.append((guard.target, info.st_dev, info.st_ino, info.st_mode))
            run.journal = operations.bind_install_roots(run.journal, tuple(roots))
    with (
        run.checkpoint(
            "secrets-and-bootstrap",
            operations.CheckpointKind.REVERSIBLE,
            paths=checkpoint_paths,
        )
        if checkpoint_paths
        else nullcontext()
    ):
        _apply_secret_plan(secret_plan)
        deploy.bootstrap_local(bootstrap)


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
