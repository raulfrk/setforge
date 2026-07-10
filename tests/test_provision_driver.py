"""Tests for the reconcile driver (spec §3).

Receipt-agnostic. Small fake :class:`Provisioner` subclasses with spies
prove: REPORT gates before any apply (zero writes), per-item HARD failure is
contained (others still applied), and exit_code is a terminal any(HARD).
"""

from collections.abc import Sequence

from setforge.config import ReconcilePolicy
from setforge.errors import ProvisionItemFailed
from setforge.provision.driver import exit_code, reconcile
from setforge.provision.protocol import (
    DesiredState,
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
)


def _item(key: str) -> ProvisionItem:
    return ProvisionItem(
        type="reference",
        identity=Identity(key=key, display=key),
        desired=DesiredState.ACTIVE,
    )


class _FakeProvisioner(Provisioner):
    """Spy provisioner: records apply_one calls; per-key outcome scripted."""

    type = "reference"

    def __init__(
        self,
        *,
        installed: set[Identity] | None = None,
        to_install: tuple[Identity, ...] = (),
        outcomes: dict[str, Outcome] | None = None,
        raises: dict[str, Outcome] | None = None,
    ) -> None:
        self._installed = installed or set()
        self._to_install = to_install
        self._outcomes = outcomes or {}
        self._raises = raises or {}
        self.apply_calls: list[str] = []

    def probe(self) -> set[Identity]:
        return self._installed

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        return ProvisionDelta(installed=self._to_install)

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        self.apply_calls.append(item.identity.key)
        if item.identity.key in self._raises:
            raise ProvisionItemFailed(
                item_id=item.identity.key,
                error_summary="boom",
                full_stderr="full boom",
                kind=self._raises[item.identity.key],
            )
        outcome = self._outcomes.get(item.identity.key, Outcome.SKIP)
        return ProvisionOutcome(item=item, outcome=outcome)

    def uninstall_one(self, identity: Identity) -> None:  # pragma: no cover
        raise AssertionError("driver must never call uninstall_one")


def test_report_policy_gates_before_apply() -> None:
    ids = (Identity(key="a", display="a"),)
    prov = _FakeProvisioner(to_install=ids)
    items = [_item("a")]
    result = reconcile(prov, items, policy=ReconcilePolicy.REPORT)
    assert result.reported is True
    assert prov.apply_calls == []
    assert result.outcomes == ()


def test_report_only_flag_gates_before_apply() -> None:
    ids = (Identity(key="a", display="a"),)
    prov = _FakeProvisioner(to_install=ids)
    result = reconcile(
        prov, [_item("a")], policy=ReconcilePolicy.ADDITIVE, report_only=True
    )
    assert result.reported is True
    assert prov.apply_calls == []


def test_hard_failure_contained_others_applied() -> None:
    ids = tuple(Identity(key=k, display=k) for k in ("a", "b", "c"))
    prov = _FakeProvisioner(
        to_install=ids,
        outcomes={"a": Outcome.SKIP, "c": Outcome.SKIP},
        raises={"b": Outcome.HARD},
    )
    items = [_item("a"), _item("b"), _item("c")]
    result = reconcile(prov, items, policy=ReconcilePolicy.ADDITIVE)
    assert prov.apply_calls == ["a", "b", "c"]
    by_key = {o.item.identity.key: o.outcome for o in result.outcomes}
    assert by_key["b"] is Outcome.HARD
    assert by_key["a"] is Outcome.SKIP
    assert by_key["c"] is Outcome.SKIP
    assert exit_code(result) == 1


def test_empty_delta_zero_applies_exit_zero() -> None:
    prov = _FakeProvisioner(to_install=())
    result = reconcile(prov, [_item("a")], policy=ReconcilePolicy.ADDITIVE)
    assert prov.apply_calls == []
    assert result.delta.is_empty()
    assert exit_code(result) == 0


def test_soft_only_exit_zero() -> None:
    # SOFT is a RETURNED outcome (a warn), not a raise — only HARD gates exit.
    ids = (Identity(key="a", display="a"),)
    prov = _FakeProvisioner(to_install=ids, outcomes={"a": Outcome.SOFT})
    result = reconcile(prov, [_item("a")], policy=ReconcilePolicy.ADDITIVE)
    assert exit_code(result) == 0


def test_mixed_soft_and_hard_exit_one() -> None:
    ids = tuple(Identity(key=k, display=k) for k in ("a", "b"))
    prov = _FakeProvisioner(
        to_install=ids, outcomes={"a": Outcome.SOFT}, raises={"b": Outcome.HARD}
    )
    result = reconcile(prov, [_item("a"), _item("b")], policy=ReconcilePolicy.ADDITIVE)
    assert exit_code(result) == 1


def test_report_performs_zero_writes_before_gate() -> None:
    # A provisioner whose apply_one would explode proves the gate returns
    # before ANY apply/write happens under REPORT.
    class _Exploding(_FakeProvisioner):
        def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
            raise AssertionError("apply_one must not run under REPORT")

    ids = (Identity(key="a", display="a"),)
    prov = _Exploding(to_install=ids)
    result = reconcile(prov, [_item("a")], policy=ReconcilePolicy.REPORT)
    assert result.reported is True
