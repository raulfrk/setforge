"""Wire declared packages into the provisioner protocol."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby

import setforge.provision.cargo as _cargo  # noqa: F401
import setforge.provision.github_release as _github_release  # noqa: F401
import setforge.provision.go as _go  # noqa: F401
import setforge.provision.local as _local  # noqa: F401
import setforge.provision.python as _python  # noqa: F401
from setforge.config import (
    Config,
    ExtensionPackage,
    PluginPackage,
    ResolvedProfile,
)
from setforge.errors import SetforgeError
from setforge.lockfile import LockFile
from setforge.provision.bundle import execute_bundle, validate_bundle
from setforge.provision.capability_graph import (
    CapabilityGraph,
    combine_capability_graphs,
)
from setforge.provision.driver import (
    ReconcilePlan,
    apply_reconcile,
    plan_reconcile,
    validate_reconcile,
)
from setforge.provision.identity import package_identity
from setforge.provision.lock_apply import apply_lock_to_items
from setforge.provision.protocol import (
    Identity,
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

    @property
    def capability_graph(self) -> CapabilityGraph:
        """Return selected bundle graphs in one globally unique namespace."""
        return combine_capability_graphs(
            tuple(zip(self.bundles, self.bundle_graphs, strict=True))
        )


def plan_provisioning(
    cfg: Config,
    resolved: ResolvedProfile,
    *,
    lock: LockFile | None = None,
) -> ProvisioningPlan:
    """Probe every top-level provisioner once and retain its exact delta."""
    items = resolve_provision_items(cfg, resolved)
    if lock is not None:
        items = apply_lock_to_items(items, lock)
    items.sort(key=lambda it: it.type)
    batches: list[ReconcilePlan] = []
    for _type, group_iter in groupby(items, key=lambda it: it.type):
        group = list(group_iter)
        batches.append(plan_reconcile(build(group[0]), group))
    bundles = tuple(resolved.bundles)
    bundle_graphs = tuple(validate_bundle(cfg.bundles[name], cfg) for name in bundles)
    return ProvisioningPlan(
        cfg_json=cfg.model_dump_json(),
        bundles=bundles,
        bundle_graphs=bundle_graphs,
        batches=tuple(batches),
    )


def apply_provisioning(plan: ProvisioningPlan) -> list[ReconcileResult]:
    """Apply a package plan without re-probing top-level provisioners."""
    cfg = Config.model_validate_json(plan.cfg_json)
    results = [
        execute_bundle(cfg.bundles[name], cfg, graph=graph)
        for name, graph in zip(plan.bundles, plan.bundle_graphs, strict=True)
    ]
    results.extend(apply_reconcile(batch) for batch in plan.batches)
    return results


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
    cfg = Config.model_validate_json(plan.cfg_json)
    current_graphs = tuple(
        validate_bundle(cfg.bundles[name], cfg) for name in plan.bundles
    )
    if current_graphs != plan.bundle_graphs:
        raise SetforgeError("frozen bundle capability graph changed; retry")
    for batch in plan.batches:
        validate_reconcile(batch)


def resolve_provision_items(
    cfg: Config, resolved: ResolvedProfile
) -> list[ProvisionItem]:
    items: list[ProvisionItem] = []
    seen: set[Identity] = set()

    def _add(item: ProvisionItem) -> None:
        if item.identity in seen:
            return
        seen.add(item.identity)
        items.append(item)

    for ref in resolved.packages:
        pkg = cfg.packages[ref]
        # Plugin/extension packages route via reconcile_adapter, not this
        # driver — skip them here so they are not double-provisioned.
        if isinstance(pkg, PluginPackage | ExtensionPackage):
            continue
        _add(
            ProvisionItem(
                type=pkg.type.value,
                identity=package_identity(pkg),
                config=pkg,
                version=getattr(pkg, "version", None),
                checksum=getattr(pkg, "checksum", None),
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
