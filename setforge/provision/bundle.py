from __future__ import annotations

from setforge.config import (
    BundleComponent,
    BundleSpec,
    Config,
    Package,
)
from setforge.errors import ConfigError, ProvisionItemFailed
from setforge.provision.capability_graph import (
    CapabilityGraph,
    CapabilityTargetKind,
    build_capability_graph,
)
from setforge.provision.identity import package_identity, package_version
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
    ReconcileResult,
)
from setforge.provision.registry import build


def _inline_model(component: BundleComponent) -> Package | None:
    for field in BundleComponent._INLINE_FIELDS:
        value = getattr(component, field)
        if value is not None:
            return None if isinstance(value, str) else value
    raise AssertionError(  # pragma: no cover
        f"bundle component {component.id!r} declares no source"
    )


def _is_file_component(component: BundleComponent) -> bool:
    return component.file is not None


def _resolve_item(component: BundleComponent, cfg: Config) -> ProvisionItem:
    if component.package is not None:
        pkg = cfg.packages[component.package]
        return ProvisionItem(
            type=pkg.type.value,
            identity=package_identity(pkg),
            config=pkg,
            version=package_version(pkg),
            checksum=getattr(pkg, "checksum", None),
        )
    model = _inline_model(component)
    if model is None:  # pragma: no cover - plugin rejected in validate_bundle
        raise AssertionError(
            f"bundle component {component.id!r} has no provisioner-backed source"
        )
    return ProvisionItem(
        type=model.type.value,
        identity=package_identity(model),
        config=model,
        version=package_version(model),
        checksum=getattr(model, "checksum", None),
    )


def validate_bundle(bundle: BundleSpec, cfg: Config) -> CapabilityGraph:
    """Resolve and validate a bundle's complete typed capability graph."""
    graph = build_capability_graph(bundle, cfg)
    graph.groups()
    return graph


def resolve_bundle_items(bundle: BundleSpec, cfg: Config) -> tuple[ProvisionItem, ...]:
    """Return provisioner-backed bundle items in graph order."""
    graph = validate_bundle(bundle, cfg)
    components = {component.id: component for component in bundle.components}
    return tuple(
        _resolve_item(components[node.id], cfg)
        for node in graph.ordered()
        if node.target_kind is CapabilityTargetKind.PACKAGE
    )


def topo_order(bundle: BundleSpec) -> list[BundleComponent]:
    order_index = {c.id: i for i, c in enumerate(bundle.components)}
    by_id = {c.id: c for c in bundle.components}
    indegree = {c.id: 0 for c in bundle.components}
    dependents: dict[str, list[str]] = {c.id: [] for c in bundle.components}
    for component in bundle.components:
        for dep in component.depends_on:
            indegree[component.id] += 1
            dependents[dep].append(component.id)

    ready = sorted(
        (cid for cid, deg in indegree.items() if deg == 0), key=order_index.__getitem__
    )
    result: list[BundleComponent] = []
    while ready:
        cid = ready.pop(0)
        result.append(by_id[cid])
        newly_ready: list[str] = []
        for dependent in dependents[cid]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                newly_ready.append(dependent)
        # Re-sort the frontier by declaration order so the tiebreak stays stable.
        ready = sorted(ready + newly_ready, key=order_index.__getitem__)
    return result


def _ownership_skip(
    item: ProvisionItem,
    component: BundleComponent,
    package_actions: dict[tuple[str, str], tuple[str, bool]],
    satisfied: set[str],
    applied_keys: set[tuple[str, str]],
) -> ProvisionOutcome | None:
    action = package_actions.get((item.type, item.identity.key))
    if action is None or action[0] not in {"adopt", "hold"}:
        return None
    if action[1]:
        satisfied.add(component.id)
        applied_keys.add((item.type, item.identity.key))
    return ProvisionOutcome(
        item=item,
        outcome=Outcome.SKIP,
        detail="ownership metadata only"
        if action[0] == "adopt"
        else "ownership authority held",
    )


def execute_bundle(
    bundle: BundleSpec,
    cfg: Config,
    *,
    provisioner: Provisioner | None = None,
    report_only: bool = False,
    graph: CapabilityGraph | None = None,
    package_actions: dict[tuple[str, str], tuple[str, bool]] | None = None,
) -> ReconcileResult:
    validated = validate_bundle(bundle, cfg)
    if graph is not None and graph != validated:
        raise ConfigError("bundle capability graph changed after planning; retry")
    graph = validated if graph is None else graph
    if report_only:
        return _report_bundle(bundle, cfg, graph=graph)
    outcomes: list[ProvisionOutcome] = []
    installed: list[Identity] = []
    # satisfied=OK or dedup no-op; skip of an unsatisfied dep propagates transitively.
    satisfied: set[str] = set()
    applied_keys: set[tuple[str, str]] = set()
    components = {component.id: component for component in bundle.components}

    for node in graph.ordered():
        component = components[node.id]
        if node.target_kind is not CapabilityTargetKind.PACKAGE:
            # The install capability executor activates these target groups.
            satisfied.add(component.id)
            continue
        blocked = any(dep not in satisfied for dep in component.depends_on)
        item = _resolve_item(component, cfg)
        if blocked:
            outcomes.append(
                ProvisionOutcome(
                    item=item, outcome=Outcome.SKIP, detail="prerequisite not satisfied"
                )
            )
            continue
        item_key = (item.type, item.identity.key)
        if item_key in applied_keys:
            # Already-applied dedup leaf: SKIP, but satisfied so dependents proceed.
            outcomes.append(
                ProvisionOutcome(
                    item=item, outcome=Outcome.SKIP, detail="already applied"
                )
            )
            satisfied.add(component.id)
            continue
        ownership_skip = _ownership_skip(
            item, component, package_actions or {}, satisfied, applied_keys
        )
        if ownership_skip is not None:
            outcomes.append(ownership_skip)
            continue
        target = provisioner if provisioner is not None else build(item)
        outcome = _apply(target, item)
        outcomes.append(outcome)
        if outcome.outcome in (Outcome.OK, Outcome.SKIP):
            satisfied.add(component.id)
            applied_keys.add(item_key)
        if outcome.outcome is Outcome.OK:
            installed.append(item.identity)

    return ReconcileResult(
        delta=ProvisionDelta(installed=tuple(installed)),
        outcomes=tuple(outcomes),
        reported=False,
    )


def _report_bundle(
    bundle: BundleSpec, cfg: Config, *, graph: CapabilityGraph
) -> ReconcileResult:
    installed: list[Identity] = []
    seen: set[tuple[str, str]] = set()
    components = {component.id: component for component in bundle.components}
    for node in graph.ordered():
        component = components[node.id]
        if node.target_kind is not CapabilityTargetKind.PACKAGE:
            continue
        identity = _resolve_item(component, cfg).identity
        item = _resolve_item(component, cfg)
        identity = item.identity
        item_key = (item.type, identity.key)
        if item_key in seen:
            continue
        seen.add(item_key)
        installed.append(identity)
    return ReconcileResult(
        delta=ProvisionDelta(installed=tuple(installed)), outcomes=(), reported=True
    )


def _apply(provisioner: Provisioner, item: ProvisionItem) -> ProvisionOutcome:
    try:
        return provisioner.apply_one(item)
    except ProvisionItemFailed as exc:
        return ProvisionOutcome(item=item, outcome=exc.kind, detail=exc.error_summary)
