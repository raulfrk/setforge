"""Tests for the reconcile driver (spec §3).

Receipt-agnostic. Small fake :class:`Provisioner` subclasses with spies
prove: REPORT gates before any apply (zero writes), per-item HARD failure is
contained (others still applied), and exit_code is a terminal any(HARD).
"""

from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from setforge.errors import ProvisionItemFailed, SetforgeError
from setforge.provision.driver import (
    apply_reconcile,
    exit_code,
    plan_reconcile,
    reconcile,
    validate_reconcile,
)
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
        self.probe_calls = 0
        self.plan_calls = 0

    def probe(self) -> set[Identity]:
        self.probe_calls += 1
        return self._installed

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        self.plan_calls += 1
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

    def uninstall_one(self, identity: Identity) -> None:
        raise AssertionError("driver must never call uninstall_one")


def test_report_policy_gates_before_apply() -> None:
    ids = (Identity(key="a", display="a"),)
    prov = _FakeProvisioner(to_install=ids)
    items = [_item("a")]
    result = reconcile(prov, items, report_only=True)
    assert result.reported is True
    assert prov.apply_calls == []
    assert result.outcomes == ()


def test_apply_consumes_plan_without_reprobe_or_replan() -> None:
    identities = tuple(Identity(key=key, display=key) for key in ("a", "b"))
    provisioner = _FakeProvisioner(
        to_install=identities,
        outcomes={"a": Outcome.OK, "b": Outcome.OK},
    )
    plan = plan_reconcile(provisioner, [_item("a"), _item("b")])

    provisioner._to_install = ()
    result = apply_reconcile(plan)

    assert provisioner.probe_calls == 1
    assert provisioner.plan_calls == 1
    assert provisioner.apply_calls == ["a", "b"]
    assert result.delta.installed == identities


def test_validate_reconcile_refuses_inventory_drift() -> None:
    provisioner = _FakeProvisioner(to_install=(Identity(key="a", display="a"),))
    plan = plan_reconcile(provisioner, [_item("a")])
    provisioner._installed = {Identity(key="other", display="other")}

    with pytest.raises(SetforgeError, match="inventory changed"):
        validate_reconcile(plan)

    assert provisioner.apply_calls == []


@given(st.lists(st.text(min_size=1), min_size=1, max_size=20, unique=True))
def test_planned_selection_is_stable_for_arbitrary_item_sets(keys: list[str]) -> None:
    identities = tuple(Identity(key=key, display=key) for key in keys[::2])
    provisioner = _FakeProvisioner(
        to_install=identities,
        outcomes={key: Outcome.OK for key in keys},
    )
    plan = plan_reconcile(provisioner, [_item(key) for key in keys])
    provisioner._to_install = tuple(
        Identity(key=key, display=key) for key in keys[1::2]
    )

    apply_reconcile(plan)

    assert provisioner.apply_calls == keys[::2]
    assert provisioner.probe_calls == 1
    assert provisioner.plan_calls == 1


def test_report_only_flag_gates_before_apply() -> None:
    ids = (Identity(key="a", display="a"),)
    prov = _FakeProvisioner(to_install=ids)
    result = reconcile(prov, [_item("a")], report_only=True)
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
    result = reconcile(prov, items)
    assert prov.apply_calls == ["a", "b", "c"]
    by_key = {o.item.identity.key: o.outcome for o in result.outcomes}
    assert by_key["b"] is Outcome.HARD
    assert by_key["a"] is Outcome.SKIP
    assert by_key["c"] is Outcome.SKIP
    assert exit_code(result) == 1


def test_empty_delta_zero_applies_exit_zero() -> None:
    prov = _FakeProvisioner(to_install=())
    result = reconcile(prov, [_item("a")])
    assert prov.apply_calls == []
    assert result.delta.is_empty()
    assert exit_code(result) == 0


def test_soft_only_exit_zero() -> None:
    # SOFT is a RETURNED outcome (a warn), not a raise — only HARD gates exit.
    ids = (Identity(key="a", display="a"),)
    prov = _FakeProvisioner(to_install=ids, outcomes={"a": Outcome.SOFT})
    result = reconcile(prov, [_item("a")])
    assert exit_code(result) == 0


def test_raised_soft_recorded_soft_exit_zero() -> None:
    # A provisioner may raise ProvisionItemFailed(kind=SOFT) — the driver must
    # honor exc.kind and record SOFT (not force HARD), so exit stays 0.
    ids = (Identity(key="a", display="a"),)
    prov = _FakeProvisioner(to_install=ids, raises={"a": Outcome.SOFT})
    result = reconcile(prov, [_item("a")])
    by_key = {o.item.identity.key: o.outcome for o in result.outcomes}
    assert by_key["a"] is Outcome.SOFT
    assert exit_code(result) == 0


def test_raised_hard_still_gates_exit() -> None:
    # A raised HARD is still recorded HARD and gates exit (regression guard for
    # the exc.kind change).
    ids = (Identity(key="a", display="a"),)
    prov = _FakeProvisioner(to_install=ids, raises={"a": Outcome.HARD})
    result = reconcile(prov, [_item("a")])
    by_key = {o.item.identity.key: o.outcome for o in result.outcomes}
    assert by_key["a"] is Outcome.HARD
    assert exit_code(result) == 1


def test_mixed_soft_and_hard_exit_one() -> None:
    ids = tuple(Identity(key=k, display=k) for k in ("a", "b"))
    prov = _FakeProvisioner(
        to_install=ids, outcomes={"a": Outcome.SOFT}, raises={"b": Outcome.HARD}
    )
    result = reconcile(prov, [_item("a"), _item("b")])
    assert exit_code(result) == 1


def test_plan_is_pure_no_apply_no_writes() -> None:
    # Acceptance clause (f): plan() must perform NO writes and NO apply_one.
    # A spy whose apply_one explodes proves plan() never touches it.
    class _PlanSpy(_FakeProvisioner):
        def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
            raise AssertionError("plan() must never call apply_one")

    ids = (Identity(key="a", display="a"),)
    prov = _PlanSpy(to_install=ids)
    delta = prov.plan([_item("a")], set())
    assert isinstance(delta, ProvisionDelta)
    assert delta.installed == ids


def test_report_performs_zero_writes_before_gate() -> None:
    # A provisioner whose apply_one would explode proves the gate returns
    # before ANY apply/write happens under REPORT.
    class _Exploding(_FakeProvisioner):
        def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
            raise AssertionError("apply_one must not run under REPORT")

    ids = (Identity(key="a", display="a"),)
    prov = _Exploding(to_install=ids)
    result = reconcile(prov, [_item("a")], report_only=True)
    assert result.reported is True
