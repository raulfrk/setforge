"""Sequential DAG execution of a bundle (spec §6 bundle).

A bundle is a set of typed components joined by ``depends_on`` edges. This
module owns the three phases:

1. :func:`validate_bundle` — a deterministic lint (INV-9): reject a duplicate
   id, a ``depends_on`` that references an unknown id, an unknown ``package:``
   reference, a ``plugin`` / ``file`` source (no provisioner yet), and any cycle
   (a self-edge or a back-edge). A diamond (``a→b, a→c, b→d, c→d``) is NOT a
   cycle and passes.
2. :func:`topo_order` — order the components so every prerequisite precedes its
   dependents, with declaration order as the stable tiebreak (deterministic
   output).
3. :func:`execute_bundle` — apply each component in topo order through the
   provisioner. "Parallel" here is semantic independence only — execution is
   strictly sequential, no threads. A component that did NOT reach
   :attr:`~setforge.provision.protocol.Outcome.OK` (HARD or SOFT) transitively
   skips ALL its dependents (the prerequisite is absent); the skip is recorded
   as a distinct :attr:`~setforge.provision.protocol.Outcome.SKIP`. Only HARD
   gates the process exit (via :func:`~setforge.provision.driver.exit_code`);
   SOFT gates dependents but not exit. The returned delta records ONLY the
   components that reached OK (INV-3). An inline source and a ``package:``
   reference that resolve to the same :class:`Identity` key are the same leaf
   and applied once. An empty bundle is a clean no-op.
"""

from __future__ import annotations

from setforge.config import (
    BundleComponent,
    BundleSpec,
    Config,
    Package,
)
from setforge.errors import ConfigError, ProvisionItemFailed
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


def _package_identity(pkg: Package) -> Identity:
    """Return the match identity for a resolved :class:`Package` model.

    Mirrors ``dispatch._package_identity`` — the key is the ecosystem's own
    natural name (crate / module / repo / …) so an inline source and a
    ``package:`` reference to the same thing dedup on one key.
    """
    name = getattr(pkg, "crate", None) or getattr(pkg, "package", None)
    name = name or getattr(pkg, "module", None) or getattr(pkg, "repo", None)
    name = name or getattr(pkg, "binary", None)
    if name is None:  # pragma: no cover - exhaustive over the Package union
        raise AssertionError(f"no identity mapping for package {pkg!r}")
    return Identity(key=name, display=name)


def _inline_model(component: BundleComponent) -> Package | None:
    """Return the resolved inline :class:`Package` model for a component.

    A typed inline source (``cargo`` / ``python`` / …) yields its Package model;
    a ``plugin`` / ``file`` source yields ``None`` (raw ``str`` ref, no
    provisioner). ``package:`` references are handled by the caller. Assumes the
    exactly-one-source validator has passed.
    """
    for field in BundleComponent._INLINE_FIELDS:
        value = getattr(component, field)
        if value is not None:
            # plugin / file are raw ``str`` refs; the rest are Package models.
            return None if isinstance(value, str) else value
    # Guarded by the model's exactly-one validator, so unreachable in practice.
    raise AssertionError(  # pragma: no cover
        f"bundle component {component.id!r} declares no source"
    )


def _resolve_item(component: BundleComponent, cfg: Config) -> ProvisionItem:
    """Resolve one component to the :class:`ProvisionItem` to provision.

    Assumes :func:`validate_bundle` has passed, so ``plugin`` / ``file``
    sources — which have no provisioner — have already been rejected.
    """
    if component.package is not None:
        pkg = cfg.packages[component.package]
        return ProvisionItem(
            type=pkg.type.value,
            identity=_package_identity(pkg),
            config=pkg,
            version=getattr(pkg, "version", None),
            checksum=getattr(pkg, "checksum", None),
        )
    model = _inline_model(component)
    if model is None:  # pragma: no cover - plugin/file rejected in validate_bundle
        raise AssertionError(
            f"bundle component {component.id!r} has no provisioner-backed source"
        )
    return ProvisionItem(
        type=model.type.value,
        identity=_package_identity(model),
        config=model,
        version=getattr(model, "version", None),
        checksum=getattr(model, "checksum", None),
    )


def validate_bundle(bundle: BundleSpec, cfg: Config) -> None:
    """Lint a bundle's DAG and references (INV-9); raise :class:`ConfigError`.

    Rejects, in order: a duplicate component id; a ``depends_on`` that names an
    unknown id (dangling edge); an unknown ``package:`` reference; a ``plugin``
    or ``file`` source (banked as future work — no provisioner, so it would
    otherwise fail mid-install at build() after earlier components applied); and
    any cycle (self-edge or back-edge), found by a three-color DFS.
    """
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
        if component.plugin is not None or component.file is not None:
            source = "plugin" if component.plugin is not None else "file"
            raise ConfigError(
                f"bundle component {component.id!r} uses a {source!r} source, "
                f"which is not yet supported for bundle components"
            )

    _reject_cycle(bundle, by_id)


def _reject_cycle(bundle: BundleSpec, by_id: dict[str, BundleComponent]) -> None:
    """Raise :class:`ConfigError` on any cycle via a three-color DFS.

    WHITE = unvisited, GRAY = on the current recursion stack, BLACK = fully
    explored. An edge into a GRAY node is a back-edge (a self-edge is the
    degenerate case) — a cycle.
    """
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
    """Return the components ordered so prerequisites precede dependents.

    Kahn's algorithm with a declaration-order queue: among components whose
    prerequisites are all placed, the earliest-declared goes first, so the
    output is deterministic. Assumes the DAG is acyclic (validate first).
    """
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
    """Validate, order, and sequentially apply a bundle.

    When ``provisioner`` is given, every component is applied through it (the
    test seam that scripts HARD/SOFT/OK). When omitted, each component's
    provisioner is built from its resolved item's type via the registry, so a
    heterogeneous bundle (cargo + python + go …) each hits its own ecosystem.

    ``report_only`` mirrors the driver's REPORT gate: it validates and orders
    but applies nothing, returning a ``reported=True`` result whose delta names
    the deduped would-install identities and whose ``outcomes`` is empty.

    Returns a :class:`ReconcileResult` whose ``outcomes`` carries one entry per
    component (OK / SKIP / SOFT / HARD) and whose ``delta`` records ONLY the
    components that reached OK. Pass the result to
    :func:`~setforge.provision.driver.exit_code` for the HARD-gated exit.
    """
    validate_bundle(bundle, cfg)
    if report_only:
        return _report_bundle(bundle, cfg)
    outcomes: list[ProvisionOutcome] = []
    installed: list[Identity] = []
    # ``satisfied`` holds the ids whose prerequisite is met for their dependents
    # — a component that reached OK OR that was a dedup no-op of an already
    # -applied leaf. A dependent is skipped iff any prerequisite is NOT
    # satisfied; because we walk in topo order every prerequisite is decided
    # before its dependent, so the skip propagates transitively.
    satisfied: set[str] = set()
    applied_keys: set[str] = set()

    for component in topo_order(bundle):
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
            # An inline-vs-ref duplicate leaf already applied: record a SKIP (no
            # second install) but mark this component satisfied so its own
            # dependents still proceed.
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
        if outcome.outcome is Outcome.OK:
            satisfied.add(component.id)
            applied_keys.add(item.identity.key)
            installed.append(item.identity)

    return ReconcileResult(
        delta=ProvisionDelta(installed=tuple(installed)),
        outcomes=tuple(outcomes),
        reported=False,
    )


def _report_bundle(bundle: BundleSpec, cfg: Config) -> ReconcileResult:
    """Return the would-install delta for a bundle without applying anything."""
    installed: list[Identity] = []
    seen: set[str] = set()
    for component in topo_order(bundle):
        identity = _resolve_item(component, cfg).identity
        if identity.key in seen:
            continue
        seen.add(identity.key)
        installed.append(identity)
    return ReconcileResult(
        delta=ProvisionDelta(installed=tuple(installed)), outcomes=(), reported=True
    )


def _apply(provisioner: Provisioner, item: ProvisionItem) -> ProvisionOutcome:
    """Apply one item, containing a raised failure as one outcome (driver rule)."""
    try:
        return provisioner.apply_one(item)
    except ProvisionItemFailed as exc:
        return ProvisionOutcome(item=item, outcome=exc.kind, detail=exc.error_summary)
