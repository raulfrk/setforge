"""Wire declared packages into the provisioner protocol."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import groupby

import setforge.provision.cargo as _cargo  # noqa: F401
import setforge.provision.github_release as _github_release  # noqa: F401
import setforge.provision.go as _go  # noqa: F401
import setforge.provision.local as _local  # noqa: F401
import setforge.provision.python as _python  # noqa: F401
from setforge.config import (
    Config,
    ExtensionPackage,
    GitHubReleasePackage,
    PluginPackage,
    ResolvedProfile,
)
from setforge.errors import SetforgeError
from setforge.lockfile import LockFile
from setforge.ownership import OwnershipStore
from setforge.platform_assets import current_host_platform
from setforge.provision.bundle import (
    execute_bundle,
    resolve_bundle_items,
    validate_bundle,
)
from setforge.provision.capability_graph import (
    CapabilityGraph,
    combine_capability_graphs,
)
from setforge.provision.driver import (
    ReconcilePlan,
    apply_reconcile,
    force_reconcile,
    plan_reconcile,
    refresh_observations,
    suppress_reconcile,
    validate_reconcile,
)
from setforge.provision.identity import (
    package_artifact,
    package_identity,
    package_version,
)
from setforge.provision.lock_apply import apply_lock_to_items
from setforge.provision.ownership import (
    PackageAction,
    PackageDecision,
    decide_package,
    package_resource_id,
    publish_claim_locked,
)
from setforge.provision.protocol import (
    ObservationOrigin,
    Outcome,
    ProvisionItem,
    ReconcileResult,
)
from setforge.provision.registry import build


@dataclass(frozen=True, slots=True)
class ProvisioningPlan:
    """Package work frozen after probing and before any package write."""

    cfg_json: str
    bundles: tuple[str, ...]
    bundle_graphs: tuple[CapabilityGraph, ...]
    batches: tuple[ReconcilePlan, ...]
    bundle_batches: tuple[ReconcilePlan, ...] = ()
    ownership: tuple[PackageDecision, ...] = ()
    direct_keys: frozenset[tuple[str, str]] = frozenset()
    lock: LockFile | None = None
    platform_os: str | None = None
    platform_arch: str | None = None

    @property
    def capability_graph(self) -> CapabilityGraph:
        """Return selected bundle graphs in one globally unique namespace."""
        return combine_capability_graphs(
            tuple(zip(self.bundles, self.bundle_graphs, strict=True))
        )


def plan_provisioning(  # noqa: C901 - direct and bundle ownership share one plan
    cfg: Config,
    resolved: ResolvedProfile,
    *,
    lock: LockFile | None = None,
    ownership_store: OwnershipStore | None = None,
    owner_id: uuid.UUID | None = None,
) -> ProvisioningPlan:
    """Probe every top-level provisioner once and retain its exact delta."""
    items = resolve_provision_items(cfg, resolved)
    host = (
        current_host_platform()
        if any(
            isinstance(item.config, GitHubReleasePackage)
            and item.config.assets is not None
            for item in items
        )
        else None
    )
    if lock is not None:
        items = apply_lock_to_items(
            items,
            lock,
            platform_os=None if host is None else host.os,
            platform_arch=None if host is None else host.arch,
        )
    items.sort(key=lambda it: it.type)
    batches: list[ReconcilePlan] = []
    ownership: list[PackageDecision] = []
    for _type, group_iter in groupby(items, key=lambda it: it.type):
        group = list(group_iter)
        batch = plan_reconcile(build(group[0]), group)
        if ownership_store is not None:
            observations = {item.identity: item for item in batch.observations}
            decisions = tuple(
                decide_package(
                    item,
                    observations.get(item.identity),
                    ownership_store.read(package_resource_id(item)),
                    owner_id=owner_id or uuid.UUID(int=0),
                )
                for item in group
            )
            decisions = tuple(
                replace(
                    decision,
                    action=PackageAction.UPGRADE,
                    detail="managed provider update required",
                )
                if decision.action is PackageAction.NONE
                and decision.item.identity in batch.delta.installed
                else decision
                for decision in decisions
            )
            ownership.extend(decisions)
            batch = suppress_reconcile(
                batch,
                frozenset(
                    decision.item.identity
                    for decision in decisions
                    if decision.action in {PackageAction.ADOPT, PackageAction.HOLD}
                ),
            )
            batch = force_reconcile(
                batch,
                frozenset(
                    decision.item.identity
                    for decision in decisions
                    if decision.action is PackageAction.UPGRADE
                ),
            )
        batches.append(batch)
    bundles = tuple(resolved.bundles)
    bundle_graphs = tuple(validate_bundle(cfg.bundles[name], cfg) for name in bundles)
    if host is None:
        bundle_requires_platform = any(
            isinstance(item.config, GitHubReleasePackage)
            and item.config.assets is not None
            for name in bundles
            for item in resolve_bundle_items(cfg.bundles[name], cfg)
        )
        if bundle_requires_platform:
            host = current_host_platform()
    bundle_batches: tuple[ReconcilePlan, ...] = ()
    if ownership_store is not None:
        seen_resources = {decision.resource_id for decision in ownership}
        direct_items = {(item.type, item.identity.key): item for item in items}
        bundle_items: dict[tuple[str, str], ProvisionItem] = {}
        for name in bundles:
            resolved_bundle_items = list(resolve_bundle_items(cfg.bundles[name], cfg))
            if (
                lock is not None
                and host is None
                and any(
                    isinstance(item.config, GitHubReleasePackage)
                    and item.config.assets is not None
                    for item in resolved_bundle_items
                )
            ):
                host = current_host_platform()
            if lock is not None:
                resolved_bundle_items = apply_lock_to_items(
                    resolved_bundle_items,
                    lock,
                    platform_os=None if host is None else host.os,
                    platform_arch=None if host is None else host.arch,
                )
            for item in resolved_bundle_items:
                bundle_key = (item.type, item.identity.key)
                direct_item = direct_items.get(bundle_key)
                if direct_item is not None and (
                    direct_item.version != item.version
                    or direct_item.checksum != item.checksum
                    or direct_item.artifact != item.artifact
                    or direct_item.platform != item.platform
                    or direct_item.config.model_dump_json()
                    != item.config.model_dump_json()
                ):
                    identity = f"{item.type}:{item.identity.key}"
                    raise SetforgeError(
                        f"package identity collision for {identity}; "
                        "declarations disagree on source or integrity"
                    )
                previous = bundle_items.get(bundle_key)
                if previous is not None and (
                    previous.version != item.version
                    or previous.checksum != item.checksum
                    or previous.artifact != item.artifact
                    or previous.platform != item.platform
                    or previous.config.model_dump_json()
                    != item.config.model_dump_json()
                ):
                    identity = f"{item.type}:{item.identity.key}"
                    raise SetforgeError(
                        f"package identity collision for {identity}; "
                        "declarations disagree on source or integrity"
                    )
                bundle_items[bundle_key] = item
        planned_bundle_batches: list[ReconcilePlan] = []
        grouped = groupby(
            sorted(bundle_items.values(), key=lambda item: item.type),
            key=lambda item: item.type,
        )
        for _type, group_iter in grouped:
            group = list(group_iter)
            batch = plan_reconcile(build(group[0]), group)
            planned_bundle_batches.append(batch)
            observations = {item.identity: item for item in batch.observations}
            for item in group:
                decision = decide_package(
                    item,
                    observations.get(item.identity),
                    ownership_store.read(package_resource_id(item)),
                    owner_id=owner_id or uuid.UUID(int=0),
                )
                if (
                    decision.action is PackageAction.NONE
                    and item.identity in batch.delta.installed
                ):
                    decision = replace(
                        decision,
                        action=PackageAction.UPGRADE,
                        detail="managed provider update required",
                    )
                if decision.resource_id not in seen_resources:
                    ownership.append(decision)
                    seen_resources.add(decision.resource_id)
        bundle_batches = tuple(planned_bundle_batches)
    legacy_providers: dict[str, set[str]] = {}
    for decision in ownership:
        if (
            decision.observation is not None
            and decision.observation.origin is ObservationOrigin.LEGACY_RECEIPT
        ):
            legacy_providers.setdefault(decision.item.identity.key, set()).add(
                decision.item.type
            )
    ambiguous = {
        key: providers
        for key, providers in legacy_providers.items()
        if len(providers) > 1
    }
    if ambiguous:
        legacy_key = sorted(ambiguous)[0]
        providers = ", ".join(sorted(ambiguous[legacy_key]))
        raise SetforgeError(
            f"legacy package receipt {legacy_key!r} is ambiguous across providers "
            f"{providers}; remove or migrate the legacy receipt before adoption"
        )
    return ProvisioningPlan(
        cfg_json=cfg.model_dump_json(),
        bundles=bundles,
        bundle_graphs=bundle_graphs,
        batches=tuple(batches),
        bundle_batches=bundle_batches,
        ownership=tuple(ownership),
        direct_keys=frozenset((item.type, item.identity.key) for item in items),
        lock=lock,
        platform_os=None if host is None else host.os,
        platform_arch=None if host is None else host.arch,
    )


def apply_provisioning(plan: ProvisioningPlan) -> list[ReconcileResult]:
    """Apply a package plan without re-probing top-level provisioners."""
    cfg = Config.model_validate_json(plan.cfg_json)
    package_actions = {
        (decision.item.type, decision.item.identity.key): (
            decision.action.value,
            decision.observation is not None,
        )
        for decision in plan.ownership
    }
    for direct_key in plan.direct_keys:
        package_actions[direct_key] = ("hold", True)
    results = [
        execute_bundle(
            cfg.bundles[name],
            cfg,
            graph=graph,
            package_actions=package_actions,
            lock=plan.lock,
            platform_os=plan.platform_os,
            platform_arch=plan.platform_arch,
        )
        for name, graph in zip(plan.bundles, plan.bundle_graphs, strict=True)
    ]
    results.extend(apply_reconcile(batch) for batch in plan.batches)
    return results


def publish_installed_package_claims_locked(
    plan: ProvisioningPlan,
    results: Sequence[ReconcileResult],
    *,
    owner_id: uuid.UUID,
) -> None:
    """Record authority only for package effects that completed successfully."""
    store = OwnershipStore()
    batch_results = results[-len(plan.batches) :] if plan.batches else []
    for batch, result in zip(plan.batches, batch_results, strict=True):
        successful = {
            outcome.item.identity
            for outcome in result.outcomes
            if outcome.outcome is Outcome.OK
        }
        if not successful:
            continue
        observations = {
            observation.identity: observation
            for observation in refresh_observations(batch)
        }
        for decision in plan.ownership:
            if (
                decision.action not in {PackageAction.INSTALL, PackageAction.UPGRADE}
                or decision.item.type != batch.provider_type
                or decision.item.identity not in successful
            ):
                continue
            observation = observations.get(decision.item.identity)
            if observation is None:
                raise SetforgeError(
                    "installed package is missing from the provider inventory; retry"
                )
            publish_claim_locked(
                store,
                PackageDecision(
                    decision.item,
                    decision.resource_id,
                    observation,
                    decision.claim,
                    decision.action,
                    decision.detail,
                ),
                owner_id=owner_id,
                declaration_ref=(
                    f"packages.{decision.item.type}.{decision.item.identity.key}"
                ),
                acquisition=(
                    "setforge-installed"
                    if decision.action is PackageAction.INSTALL
                    else "setforge-upgraded"
                ),
            )
    bundle_results = results[: len(plan.bundles)]
    successful_bundle = {
        (outcome.item.type, outcome.item.identity.key)
        for result in bundle_results
        for outcome in result.outcomes
        if outcome.outcome is Outcome.OK
    }
    if successful_bundle:
        bundle_observations = {
            (batch.provider_type, observation.identity.key): observation
            for batch in plan.bundle_batches
            for observation in refresh_observations(batch)
        }
        for decision in plan.ownership:
            bundle_key = (decision.item.type, decision.item.identity.key)
            if (
                decision.action not in {PackageAction.INSTALL, PackageAction.UPGRADE}
                or bundle_key not in successful_bundle
            ):
                continue
            observation = bundle_observations.get(bundle_key)
            if observation is None:
                raise SetforgeError(
                    "installed bundle package is missing from provider inventory; retry"
                )
            publish_claim_locked(
                store,
                PackageDecision(
                    decision.item,
                    decision.resource_id,
                    observation,
                    decision.claim,
                    decision.action,
                    decision.detail,
                ),
                owner_id=owner_id,
                declaration_ref=(
                    f"packages.{decision.item.type}.{decision.item.identity.key}"
                ),
                acquisition=(
                    "setforge-installed"
                    if decision.action is PackageAction.INSTALL
                    else "setforge-upgraded"
                ),
            )


def report_provisioning(plan: ProvisioningPlan) -> list[ReconcileResult]:
    """Return report-only results from the same frozen package plan."""
    cfg = Config.model_validate_json(plan.cfg_json)
    results = [
        execute_bundle(cfg.bundles[name], cfg, report_only=True, graph=graph)
        for name, graph in zip(plan.bundles, plan.bundle_graphs, strict=True)
    ]
    results.extend(
        ReconcileResult(delta=batch.delta, outcomes=(), reported=True)
        for batch in plan.batches
    )
    return results


def validate_provisioning(plan: ProvisioningPlan) -> None:
    """Refuse when any top-level package inventory changed after planning."""
    if plan.platform_os is not None and plan.platform_arch is not None:
        host = current_host_platform()
        if (host.os, host.arch) != (plan.platform_os, plan.platform_arch):
            raise SetforgeError("package platform changed after planning; retry")
    cfg = Config.model_validate_json(plan.cfg_json)
    current_graphs = tuple(
        validate_bundle(cfg.bundles[name], cfg) for name in plan.bundles
    )
    if current_graphs != plan.bundle_graphs:
        raise SetforgeError("frozen bundle capability graph changed; retry")
    for batch in plan.batches:
        validate_reconcile(batch)
    for batch in plan.bundle_batches:
        validate_reconcile(batch)


def resolve_provision_items(
    cfg: Config, resolved: ResolvedProfile
) -> list[ProvisionItem]:
    items: list[ProvisionItem] = []
    seen: dict[tuple[str, str], ProvisionItem] = {}

    def _add(item: ProvisionItem) -> None:
        key = (item.type, item.identity.key)
        if key in seen:
            previous = seen[key]
            if (
                previous.version != item.version
                or previous.checksum != item.checksum
                or previous.artifact != item.artifact
                or previous.platform != item.platform
                or previous.config.model_dump_json() != item.config.model_dump_json()
            ):
                raise SetforgeError(
                    f"package identity collision for {item.type}:{item.identity.key}; "
                    "declarations disagree on source or integrity"
                )
            return
        seen[key] = item
        items.append(item)

    for ref in resolved.packages:
        pkg = cfg.packages[ref]
        # Plugin/extension packages route via reconcile_adapter, not this
        # driver — skip them here so they are not double-provisioned.
        if isinstance(pkg, PluginPackage | ExtensionPackage):
            continue
        artifact, platform, checksum = package_artifact(pkg)
        _add(
            ProvisionItem(
                type=pkg.type.value,
                identity=package_identity(pkg),
                config=pkg,
                version=package_version(pkg),
                checksum=checksum,
                artifact=artifact,
                platform=platform,
            )
        )
    return items


def run_provisioning(
    cfg: Config,
    resolved: ResolvedProfile,
    *,
    report_only: bool = False,
    lock: LockFile | None = None,
) -> list[ReconcileResult]:
    plan = plan_provisioning(cfg, resolved, lock=lock)
    return report_provisioning(plan) if report_only else apply_provisioning(plan)


def has_hard_failure(results: Sequence[ReconcileResult]) -> bool:
    return any(o.outcome is Outcome.HARD for result in results for o in result.outcomes)
