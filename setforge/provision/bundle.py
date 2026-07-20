from __future__ import annotations

from setforge.config import (
    BundleComponent,
    BundleSpec,
    Config,
    Package,
)
from setforge.errors import ConfigError, ProvisionItemFailed
from setforge.provision.identity import package_identity
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
            version=getattr(pkg, "version", None),
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
        version=getattr(model, "version", None),
        checksum=getattr(model, "checksum", None),
    )


def validate_bundle(bundle: BundleSpec, cfg: Config) -> None:
    ids: list[str] = [c.id for c in bundle.components]
    seen: set[str] = set()
    for cid in ids:
        if cid in seen:
            raise ConfigError(f"bundle has a duplicate component id: {cid!r}")
        seen.add(cid)

    by_id = {c.id: c for c in bundle.components}
    for component in bundle.components:
        for dep in component.depends_on:
            if dep not in by_id:
                raise ConfigError(
                    f"bundle component {component.id!r} depends_on unknown id {dep!r}"
                )
        if component.package is not None and component.package not in cfg.packages:
            raise ConfigError(
                f"bundle component {component.id!r} references unknown "
                f"package {component.package!r}"
            )
        if component.plugin is not None:
            raise ConfigError(
                f"bundle component {component.id!r} uses a 'plugin' source, "
                f"which is not yet supported for bundle components"
            )
        if _is_file_component(component) and component.depends_on:
            # A file component is deploy-only: it is expanded into a synthetic
            # tracked_file and deployed by the tracked-file pipeline, never
            # reaching the provisioner driver (execute_bundle skips it). So it
            # cannot gate on a provisioner prerequisite — a depends_on on it is
            # silently unhonored. Refuse the nonsensical shape up front rather
            # than accept a dependency the deploy side structurally ignores.
            raise ConfigError(
                f"bundle component {component.id!r} is a file component and "
                f"must not declare depends_on: a file component is deploy-only "
                f"(handled by the tracked-file pipeline) and never participates "
                f"in the provisioner dependency gate"
            )

    _reject_cycle(bundle, by_id)


def _reject_cycle(bundle: BundleSpec, by_id: dict[str, BundleComponent]) -> None:
    """3-color DFS: edge into a GRAY (on-stack) node is a cycle (self-edge included)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {cid: WHITE for cid in by_id}

    def visit(cid: str) -> None:
        color[cid] = GRAY
        for dep in by_id[cid].depends_on:
            if color[dep] == GRAY:
                raise ConfigError(
                    f"bundle has a dependency cycle involving {cid!r} and {dep!r}"
                )
            if color[dep] == WHITE:
                visit(dep)
        color[cid] = BLACK

    for component in bundle.components:
        if color[component.id] == WHITE:
            visit(component.id)


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


def execute_bundle(
    bundle: BundleSpec,
    cfg: Config,
    *,
    provisioner: Provisioner | None = None,
    report_only: bool = False,
) -> ReconcileResult:
    validate_bundle(bundle, cfg)
    if report_only:
        return _report_bundle(bundle, cfg)
    outcomes: list[ProvisionOutcome] = []
    installed: list[Identity] = []
    # satisfied=OK or dedup no-op; skip of an unsatisfied dep propagates transitively.
    satisfied: set[str] = set()
    applied_keys: set[str] = set()

    for component in topo_order(bundle):
        if _is_file_component(component):
            # Deploy-only; mark satisfied so downstream package deps proceed.
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
        if item.identity.key in applied_keys:
            # Already-applied dedup leaf: SKIP, but satisfied so dependents proceed.
            outcomes.append(
                ProvisionOutcome(
                    item=item, outcome=Outcome.SKIP, detail="already applied"
                )
            )
            satisfied.add(component.id)
            continue
        target = provisioner if provisioner is not None else build(item)
        outcome = _apply(target, item)
        outcomes.append(outcome)
        if outcome.outcome in (Outcome.OK, Outcome.SKIP):
            satisfied.add(component.id)
            applied_keys.add(item.identity.key)
        if outcome.outcome is Outcome.OK:
            installed.append(item.identity)

    return ReconcileResult(
        delta=ProvisionDelta(installed=tuple(installed)),
        outcomes=tuple(outcomes),
        reported=False,
    )


def _report_bundle(bundle: BundleSpec, cfg: Config) -> ReconcileResult:
    installed: list[Identity] = []
    seen: set[str] = set()
    for component in topo_order(bundle):
        if _is_file_component(component):
            continue
        identity = _resolve_item(component, cfg).identity
        if identity.key in seen:
            continue
        seen.add(identity.key)
        installed.append(identity)
    return ReconcileResult(
        delta=ProvisionDelta(installed=tuple(installed)), outcomes=(), reported=True
    )


def _apply(provisioner: Provisioner, item: ProvisionItem) -> ProvisionOutcome:
    try:
        return provisioner.apply_one(item)
    except ProvisionItemFailed as exc:
        return ProvisionOutcome(item=item, outcome=exc.kind, detail=exc.error_summary)
