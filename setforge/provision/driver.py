"""The central reconcile driver (spec §3).

The single choke point owning every cross-cutting invariant so a new
provisioner cannot get exit-gating or the REPORT-no-write gate wrong:

* the REPORT gate is control-flow (an early return before any apply), never
  a boolean threaded into leaf calls;
* per-item apply failure is contained — one :class:`ProvisionItemFailed`
  records one outcome of the raised ``kind`` (SOFT or HARD) and the loop
  continues;
* :func:`exit_code` is a terminal ``any(HARD)`` reduction, never a scalar
  reassigned per item.

Receipt-agnostic: a marker-based provisioner writes its own receipt inside
:meth:`~setforge.provision.protocol.Provisioner.apply_one`; the driver does
not.
"""

from __future__ import annotations

import builtins
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field, replace

from pydantic import BaseModel

from setforge.errors import ProvisionItemFailed, SetforgeError
from setforge.provision.protocol import (
    DesiredState,
    Identity,
    Outcome,
    PackageObservation,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
    ReconcileResult,
)


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    """Immutable decisions plus a private executor capsule for apply."""

    delta: ProvisionDelta
    installed: frozenset[Identity]
    observations: tuple[PackageObservation, ...]
    provider_type: str
    _inventory_fingerprint: Hashable = field(repr=False)
    _executor: _ReconcileExecutor = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _ReconcileExecutor:
    """Private frozen capability; never part of the plan's value surface."""

    provisioner: Provisioner
    items: tuple[_FrozenProvisionItem, ...]
    declared_items: tuple[_FrozenProvisionItem, ...]


@dataclass(frozen=True, slots=True)
class _FrozenProvisionItem:
    """Serialized item snapshot detached from the caller's Pydantic model."""

    type: str
    identity: Identity
    desired: DesiredState
    version: str | None
    checksum: str | None
    config_type: builtins.type[BaseModel]
    config_json: str

    @classmethod
    def from_item(cls, item: ProvisionItem) -> _FrozenProvisionItem:
        return cls(
            type=item.type,
            identity=item.identity,
            desired=item.desired,
            version=item.version,
            checksum=item.checksum,
            config_type=type(item.config),
            config_json=item.config.model_dump_json(),
        )

    def thaw(self) -> ProvisionItem:
        """Build a fresh mutable model only for the immediate apply call."""
        return ProvisionItem(
            type=self.type,
            identity=self.identity,
            desired=self.desired,
            version=self.version,
            checksum=self.checksum,
            config=self.config_type.model_validate_json(self.config_json),
        )


def plan_reconcile(
    provisioner: Provisioner, items: Sequence[ProvisionItem]
) -> ReconcilePlan:
    """Probe once and freeze the resulting apply selection."""
    installed = provisioner.probe()
    inventory_fingerprint = provisioner.plan_fingerprint(items, installed)
    delta = provisioner.plan(items, installed)
    observations = provisioner.observations(installed)
    return ReconcilePlan(
        delta=delta,
        installed=frozenset(installed),
        observations=observations,
        provider_type=provisioner.type,
        _inventory_fingerprint=inventory_fingerprint,
        _executor=_ReconcileExecutor(
            provisioner=provisioner,
            items=tuple(
                _FrozenProvisionItem.from_item(item)
                for item in _items_to_apply(delta, items)
            ),
            declared_items=tuple(
                _FrozenProvisionItem.from_item(item) for item in items
            ),
        ),
    )


def validate_reconcile(plan: ReconcilePlan) -> None:
    """Refuse when the global provisioner inventory changed after planning."""
    provisioner = plan._executor.provisioner
    installed = provisioner.probe()
    observations = provisioner.observations(installed)
    items = tuple(item.thaw() for item in plan._executor.declared_items)
    if (
        provisioner.plan_fingerprint(items, installed) != plan._inventory_fingerprint
        or observations != plan.observations
    ):
        raise SetforgeError("package inventory changed after planning; retry")


def suppress_reconcile(
    plan: ReconcilePlan, identities: frozenset[Identity]
) -> ReconcilePlan:
    """Return the same frozen plan with selected package effects held."""
    if not identities:
        return plan
    executor = replace(
        plan._executor,
        items=tuple(
            item for item in plan._executor.items if item.identity not in identities
        ),
    )
    return replace(
        plan,
        delta=ProvisionDelta(
            installed=tuple(
                identity
                for identity in plan.delta.installed
                if identity not in identities
            ),
            activated=tuple(
                identity
                for identity in plan.delta.activated
                if identity not in identities
            ),
        ),
        _executor=executor,
    )


def force_reconcile(
    plan: ReconcilePlan, identities: frozenset[Identity]
) -> ReconcilePlan:
    """Return a frozen plan that applies selected managed upgrades."""
    if not identities:
        return plan
    selected = tuple(
        item for item in plan._executor.declared_items if item.identity in identities
    )
    existing = {item.identity for item in plan._executor.items}
    executor = replace(
        plan._executor,
        items=plan._executor.items
        + tuple(item for item in selected if item.identity not in existing),
    )
    installed = tuple(dict.fromkeys((*plan.delta.installed, *identities)))
    return replace(
        plan,
        delta=ProvisionDelta(
            installed=installed,
            activated=plan.delta.activated,
        ),
        _executor=executor,
    )


def refresh_observations(plan: ReconcilePlan) -> tuple[PackageObservation, ...]:
    """Re-probe one frozen provider after successful package effects."""
    installed = plan._executor.provisioner.probe()
    return plan._executor.provisioner.observations(installed)


def apply_reconcile(plan: ReconcilePlan) -> ReconcileResult:
    """Apply an existing plan without probing or planning again."""
    outcomes: list[ProvisionOutcome] = []
    for frozen_item in plan._executor.items:
        item = frozen_item.thaw()
        try:
            outcomes.append(plan._executor.provisioner.apply_one(item))
        except ProvisionItemFailed as exc:
            outcomes.append(
                ProvisionOutcome(item=item, outcome=exc.kind, detail=exc.error_summary)
            )
    return ReconcileResult(delta=plan.delta, outcomes=tuple(outcomes), reported=False)


def _items_to_apply(
    delta: ProvisionDelta, items: Sequence[ProvisionItem]
) -> list[ProvisionItem]:
    """Select the declared items the plan says to act on.

    An item acts when its identity appears in the delta's ``installed`` or
    ``activated`` set (matched by identity ``==``, i.e. on ``key``). Original
    declaration order is preserved.
    """
    targets = set(delta.installed) | set(delta.activated)
    return [item for item in items if item.identity in targets]


def reconcile(
    provisioner: Provisioner,
    items: Sequence[ProvisionItem],
    *,
    report_only: bool = False,
) -> ReconcileResult:
    """Reconcile ``items`` against reality through ``provisioner``.

    Probes (read-only), plans (pure), then — unless ``report_only`` is set —
    applies each planned item, containing any per-item failure as one outcome
    of the raised ``kind`` (SOFT or HARD). Returns the delta plus the recorded
    outcomes.
    """
    plan = plan_reconcile(provisioner, items)
    if report_only:
        return ReconcileResult(delta=plan.delta, outcomes=(), reported=True)
    return apply_reconcile(plan)


def exit_code(result: ReconcileResult) -> int:
    """Return 1 iff any outcome is HARD, else 0 (a terminal ``any`` reduction)."""
    return 1 if any(o.outcome is Outcome.HARD for o in result.outcomes) else 0
