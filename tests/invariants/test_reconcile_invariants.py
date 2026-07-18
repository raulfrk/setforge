from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import HealthCheck, event, settings
from hypothesis.errors import InvalidArgument
from hypothesis.stateful import rule, run_state_machine_as_test

from setforge.config import Config
from setforge.errors import InvariantViolation as StoreInvariantViolation
from tests.harness import strategies as hstrat
from tests.harness.invariants import (
    InvariantStateMachine,
    InvariantViolation,
    invariant,
)
from tests.harness.model import StubReconcileModel

# too_slow is suppressed for real fsync-backed I/O latency, not to mask a
# subprocess escape (the machine's own guard already raises on any shell-out).
_REAL_RUN = settings(
    max_examples=60,
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _stat(label: str) -> None:
    # event() raises outside a Hypothesis build context; swallow for standalone use.
    with contextlib.suppress(InvalidArgument):
        event(label)


class ReconcileInvariantMachine(InvariantStateMachine):
    model_factory = staticmethod(StubReconcileModel.create)

    @rule(config=hstrat.configs())
    def reconfigure(self, config: Config) -> None:
        self.model.set_config(config)

    @rule()
    def install(self) -> None:
        self.model.install()

    @rule()
    def sync(self) -> None:
        self.model.sync()

    @rule()
    def migrate(self) -> None:
        self.model.migrate()

    @rule()
    def revert(self) -> None:
        self.model.revert()

    @invariant()
    def inv1_no_silent_data_loss(self) -> None:
        live = self.model.live_bodies()
        _stat(f"inv1: live files = {len(live)}")
        for fid, body in live.items():
            reconstructed = self.model.base_plus_local(fid)
            assert reconstructed is not None, f"INV-1: {fid} live but not in the store"
            assert reconstructed == body.encode("utf-8"), (
                f"INV-1: {fid} live body diverges from its store reconstruction"
            )

    @invariant()
    def inv2_inv10_store_consistent(self) -> None:
        _stat(f"inv2/10: indexed files = {len(self.model.indexed_file_ids())}")
        try:
            self.model.verify_store()
        except StoreInvariantViolation as exc:
            raise InvariantViolation(
                f"INV-2/INV-10 store verify failed: {exc}"
            ) from exc


def test_reconcile_invariant_machine() -> None:
    run_state_machine_as_test(ReconcileInvariantMachine, settings=_REAL_RUN)


TestReconcileInvariantMachine = ReconcileInvariantMachine.TestCase


def test_revert_install_is_byte_exact_inverse(isolated_model_root) -> None:
    model = StubReconcileModel.create(isolated_model_root)
    model.set_config(hstrat.minimal_config(schema_version="1.2"))
    model.install()
    before_index = model.store_index()
    before_live = model.live_bodies()
    model.sync()
    model.revert()
    assert model.store_index() == before_index, "INV-3: revert did not restore index"
    assert model.live_bodies() == before_live, "INV-3: revert did not restore live"


def test_migrate_then_revert_is_identity(isolated_model_root) -> None:
    model = StubReconcileModel.create(isolated_model_root)
    model.set_config(hstrat.minimal_config(schema_version="1.2"))
    before = model.schema_version()
    model.migrate()
    assert model.schema_version() != before, "migrate must advance the schema first"
    model.revert()
    assert model.schema_version() == before, "INV-5: revert ∘ migrate is not identity"


def test_install_is_idempotent(isolated_model_root) -> None:
    model = StubReconcileModel.create(isolated_model_root)
    model.set_config(hstrat.minimal_config(schema_version="1.2"))
    model.install()
    first_index = model.store_index()
    first_live = model.live_bodies()
    assert first_index, "precondition: the first install recorded a store index"
    model.install()
    assert model.store_index() == first_index, "INV-4: re-install changed the index"
    assert model.live_bodies() == first_live, "INV-4: re-install changed live bodies"


@pytest.fixture
def driven_machine(
    isolated_model_root: Path,
) -> Iterator[ReconcileInvariantMachine]:
    machine = ReconcileInvariantMachine(isolated_model_root)
    machine.model.set_config(hstrat.minimal_config(schema_version="1.2"))
    machine.model.install()
    yield machine
    machine.teardown()


def test_inv1_fires_on_nonempty_live(driven_machine) -> None:
    assert driven_machine.model.live_bodies(), "precondition: live is non-empty"
    driven_machine.inv1_no_silent_data_loss()


def test_inv2_inv10_fires_on_nonempty_index(driven_machine) -> None:
    assert driven_machine.model.indexed_file_ids(), "precondition: index non-empty"
    driven_machine.inv2_inv10_store_consistent()


def test_inv2_inv10_catches_a_seeded_orphan(driven_machine) -> None:
    from setforge.reconcile import store as reconcile_store

    model = driven_machine.model
    fid = next(iter(model.indexed_file_ids()))
    reconcile_store.local_content_path(model.profile, fid).unlink()
    with pytest.raises(InvariantViolation, match="INV-2/INV-10"):
        driven_machine.inv2_inv10_store_consistent()


def test_inv1_catches_a_seeded_violation(driven_machine) -> None:
    model = driven_machine.model
    fid = next(iter(model.live_bodies()))
    model.live[fid] = model.live[fid] + "\n# tampered, not in the store\n"
    with pytest.raises(AssertionError, match="INV-1"):
        driven_machine.inv1_no_silent_data_loss()


def scoped_out_invariants() -> dict[str, str]:
    return {
        "INV-6": (
            "merge engine's own fail-closed _verify; asserted in "
            "tests/reconcile/test_merge_properties.py::"
            "test_two_sided_independent_edits_positional (generative edits-only "
            "vs positional-overlap case the multiset _verify cannot see)"
        ),
        "INV-7": (
            "provisioner shells out (claude/code/gitleaks) — not hermetic here; "
            "asserted end-to-end in tests/test_provision_reference.py "
            "(test_hard_failure_gates_exit_others_applied → nonzero exit, "
            "test_soft_only_exits_zero → exit 0, "
            "test_report_policy_performs_no_installs → zero writes, "
            "test_second_run_is_idempotent_noop → empty delta)"
        ),
        "INV-8": (
            "needs the share/keep staging verb, not driven by this machine; "
            "tracked must equal the reconstruct of exactly the promoted set — "
            "asserted in tests/reconcile/test_hunks.py::"
            "test_fidelity_raises_when_local_bytes_leak_into_tracked and raised "
            "in setforge/reconcile/structured_units.py "
            "(assert_stage_fidelity_structured, "
            'the "INV-8: tracked content is not exactly the shared key-unit set" '
            "message)"
        ),
        "INV-9": (
            "pure bundle-model DAG property, not a reconcile-verb step; "
            "asserted in tests/test_provision_bundle.py "
            "(test_self_edge_rejected, test_back_edge_cycle_rejected, "
            "test_diamond_is_not_a_cycle, test_dangling_depends_on_rejected, "
            "test_topo_order_honors_depends_on over the depends_on DAG)"
        ),
    }


def test_scope_out_is_documented() -> None:
    scoped = scoped_out_invariants()
    assert set(scoped) == {"INV-6", "INV-7", "INV-8", "INV-9"}
    assert all(reason for reason in scoped.values())
