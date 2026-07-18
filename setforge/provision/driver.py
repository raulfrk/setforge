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

from collections.abc import Sequence

from setforge.errors import ProvisionItemFailed
from setforge.provision.protocol import (
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
    ReconcileResult,
)


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
    installed = provisioner.probe()
    delta = provisioner.plan(items, installed)
    if report_only:
        return ReconcileResult(delta=delta, outcomes=(), reported=True)
    outcomes: list[ProvisionOutcome] = []
    for item in _items_to_apply(delta, items):
        try:
            outcomes.append(provisioner.apply_one(item))
        except ProvisionItemFailed as exc:
            outcomes.append(
                ProvisionOutcome(item=item, outcome=exc.kind, detail=exc.error_summary)
            )
    return ReconcileResult(delta=delta, outcomes=tuple(outcomes), reported=False)


def exit_code(result: ReconcileResult) -> int:
    """Return 1 iff any outcome is HARD, else 0 (a terminal ``any`` reduction)."""
    return 1 if any(o.outcome is Outcome.HARD for o in result.outcomes) else 0
