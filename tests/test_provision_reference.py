"""Acceptance proof for the reference provisioner (spec §6, §8).

End-to-end through the real driver: partial HARD ⇒ nonzero exit (others still
applied), SOFT-only ⇒ exit 0, REPORT ⇒ zero writes, a second identical run ⇒
empty delta (INV-7 idempotent skip), and a receipt-backed variant proving the
marker path survives a fresh provisioner on the same receipt root.
"""

from pathlib import Path

from setforge.config import ReconcilePolicy
from setforge.provision.driver import exit_code, reconcile
from setforge.provision.protocol import (
    DesiredState,
    Identity,
    ProvisionItem,
)
from setforge.provision.receipt import ReceiptStore
from setforge.provision.reference import InMemoryProvisioner


def _item(key: str) -> ProvisionItem:
    return ProvisionItem(
        type="reference",
        identity=Identity(key=key, display=key),
        desired=DesiredState.ACTIVE,
    )


def test_hard_failure_gates_exit_others_applied() -> None:
    prov = InMemoryProvisioner(fail_hard={"b"})
    items = [_item("a"), _item("b"), _item("c")]
    result = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    assert exit_code(result) == 1
    installed = {ident.key for ident in prov.probe()}
    assert installed == {"a", "c"}


def test_soft_only_exits_zero() -> None:
    prov = InMemoryProvisioner(soft={"a"})
    result = reconcile(prov, [_item("a")], policy=ReconcilePolicy.ADDITIVE)
    assert exit_code(result) == 0
    assert prov.probe() == set()


def test_report_policy_performs_no_installs() -> None:
    prov = InMemoryProvisioner()
    before = set(prov.probe())
    result = reconcile(prov, [_item("a")], policy=ReconcilePolicy.REPORT)
    assert result.reported is True
    assert prov.probe() == before


def test_second_run_is_idempotent_noop() -> None:
    prov = InMemoryProvisioner()
    items = [_item("a"), _item("b")]
    first = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    assert first.delta.is_empty() is False
    second = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    assert second.delta.is_empty() is True
    assert second.outcomes == ()


def test_receipt_backed_idempotency(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path)
    prov = InMemoryProvisioner(receipts=store)
    items = [_item("a")]
    reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    assert {ident.key for ident in store.installed()} == {"a"}

    fresh = InMemoryProvisioner(receipts=ReceiptStore(tmp_path))
    assert {ident.key for ident in fresh.probe()} == {"a"}
    second = reconcile(fresh, items, policy=ReconcilePolicy.ADDITIVE)
    assert second.delta.is_empty() is True
    assert second.outcomes == ()
