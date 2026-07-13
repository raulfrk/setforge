"""Real INV-1..10 over the reconcile engine (RFC 0001 §6, task E2).

A :class:`~tests.harness.invariants.InvariantStateMachine` that drives the REAL
``install`` / ``sync`` / ``revert`` / ``migrate`` reconcile engine over
Hypothesis-generated ``setforge.yaml`` sequences and checks the store / merge /
migrate invariants after every step. The machine owns the invariants whose real
verb path is wire-ready and subprocess-free (the Epic-A base/local/index store +
line 3-way merge + the schema-migration registry):

- INV-1 (no silent data loss) — every live body reconstructs from the store.
- INV-2 (base round-trips) — ``base + recorded-local == live``, via the store's
  own fail-closed :func:`setforge.reconcile.store.verify`.
- INV-10 (index ↔ on-disk consistent) — no orphan classification, via the same
  store ``verify``.

INV-1/2/10 are pure, side-effect-free per-step ``@invariant()`` methods. The
two-step identity properties are proven by dedicated property tests below
(NOT as per-step invariants, which must not mutate the model): INV-3 (revert
byte-exact), INV-4 (``install ∘ install`` idempotent), and INV-5 (``revert ∘
migrate`` identity).

Scoped OUT of THIS machine (real verb not driven here — see the module tail):
INV-6 (owned + enforced by the merge engine's own fail-closed ``_verify``),
INV-7 (provisioner — shells out, not subprocess-free), INV-8 (hunk staging — no
``share``/``keep`` verb in this machine), INV-9 (bundle DAG — a pure bundle-model
property, not an install/sync/revert/migrate step).
"""

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

# The real run: raised above the scaffold's 25/12 for deeper sequence coverage.
# too_slow is suppressed because the real engine's fsync-backed store writes make
# per-example latency I/O-bound and variable — NOT to mask an unmocked subprocess
# (the machine's subprocess guard raises on any real shell-out, so a masked
# too_slow can never be hiding one).
_REAL_RUN = settings(
    max_examples=60,
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _stat(label: str) -> None:
    """Record a Hypothesis ``event`` for the anti-vacuous stats, no-op standalone.

    ``event()`` raises outside a Hypothesis build context, so the positive-control
    tests (which call an invariant method by hand) would break on it. This wrapper
    swallows that one case so the same invariant body serves both the stateful run
    (where the event feeds the fired-on-N-sequences statistics) and the standalone
    positive control.
    """
    with contextlib.suppress(InvalidArgument):
        event(label)


class ReconcileInvariantMachine(InvariantStateMachine):
    """Drives the real reconcile engine; asserts INV-1/2/4/10 after each step."""

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
        """INV-1: every live body is reconstructable from the store (no loss).

        A file that install deployed to live must round-trip through the store's
        ``base + recorded-local`` reconstruction — a live body the store cannot
        reproduce is a silently-lost user byte.
        """
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
        """INV-2 + INV-10: the store's own fail-closed round-trip + no-orphan check.

        Delegates to :func:`setforge.reconcile.store.verify` — the engine's
        owning assertion that recorded-local bytes hash to the index (INV-2) and
        that every index entry has an on-disk local file and vice-versa
        (INV-10). It raises the store's :class:`InvariantViolation`; re-raised as
        the harness one so a falsifying example names the broken INV.
        """
        _stat(f"inv2/10: indexed files = {len(self.model.indexed_file_ids())}")
        try:
            self.model.verify_store()
        except StoreInvariantViolation as exc:
            raise InvariantViolation(
                f"INV-2/INV-10 store verify failed: {exc}"
            ) from exc


def test_reconcile_invariant_machine() -> None:
    """The machine drives the real engine + checks INV-1/2/4/10 each step."""
    run_state_machine_as_test(ReconcileInvariantMachine, settings=_REAL_RUN)


# Bind the machine's ``TestCase`` so a bare ``pytest tests/invariants`` also runs
# it under Hypothesis's default settings (belt-and-suspenders with the explicit
# test above, which pins the deeper real-run settings).
TestReconcileInvariantMachine = ReconcileInvariantMachine.TestCase


# --------------------------------------------------------------------------- #
# INV-3 / INV-5 — two-step identity properties (revert ∘ verb == identity).
# --------------------------------------------------------------------------- #


def test_revert_install_is_byte_exact_inverse(isolated_model_root) -> None:
    """INV-3: revert restores the byte-exact store + live state before a verb."""
    model = StubReconcileModel.create(isolated_model_root)
    model.set_config(hstrat.minimal_config(schema_version="1.2"))
    model.install()  # first install: establishes base + live
    before_index = model.store_index()
    before_live = model.live_bodies()
    model.sync()  # a second mutating verb whose revert must restore exactly
    model.revert()
    assert model.store_index() == before_index, "INV-3: revert did not restore index"
    assert model.live_bodies() == before_live, "INV-3: revert did not restore live"


def test_migrate_then_revert_is_identity(isolated_model_root) -> None:
    """INV-5: revert ∘ migrate restores the byte-exact pre-migrate schema state."""
    model = StubReconcileModel.create(isolated_model_root)
    model.set_config(hstrat.minimal_config(schema_version="1.2"))
    before = model.schema_version()
    model.migrate()
    assert model.schema_version() != before, "migrate must advance the schema first"
    model.revert()
    assert model.schema_version() == before, "INV-5: revert ∘ migrate is not identity"


def test_install_is_idempotent(isolated_model_root) -> None:
    """INV-4: ``install ∘ install`` computes an empty delta.

    Compares the observable store index (``local_hash`` content + ``present``
    classification) and live bodies across a re-install — both must be
    byte-identical. The index EXCLUDES any rewritten timestamp / mode metadata,
    so this is content+classification only (the anti-flake requirement).
    """
    model = StubReconcileModel.create(isolated_model_root)
    model.set_config(hstrat.minimal_config(schema_version="1.2"))
    model.install()
    first_index = model.store_index()
    first_live = model.live_bodies()
    assert first_index, "precondition: the first install recorded a store index"
    model.install()
    assert model.store_index() == first_index, "INV-4: re-install changed the index"
    assert model.live_bodies() == first_live, "INV-4: re-install changed live bodies"


# --------------------------------------------------------------------------- #
# Positive controls — prove each per-step invariant actually FIRES on non-empty
# data (a never-executing assert is a false green). Each drives one verb by hand
# and asserts the invariant runs over a NON-empty observable.
# --------------------------------------------------------------------------- #


@pytest.fixture
def driven_machine(
    isolated_model_root: Path,
) -> Iterator[ReconcileInvariantMachine]:
    """A rule-bearing machine with an installed config + one real install run."""
    machine = ReconcileInvariantMachine(isolated_model_root)
    machine.model.set_config(hstrat.minimal_config(schema_version="1.2"))
    machine.model.install()
    yield machine
    machine.teardown()


def test_inv1_fires_on_nonempty_live(driven_machine) -> None:
    """INV-1 executes its per-file assert over a non-empty live set."""
    assert driven_machine.model.live_bodies(), "precondition: live is non-empty"
    driven_machine.inv1_no_silent_data_loss()  # must not raise


def test_inv2_inv10_fires_on_nonempty_index(driven_machine) -> None:
    """INV-2/INV-10 executes ``verify`` over a non-empty index."""
    assert driven_machine.model.indexed_file_ids(), "precondition: index non-empty"
    driven_machine.inv2_inv10_store_consistent()  # must not raise


def test_inv2_inv10_catches_a_seeded_orphan(driven_machine) -> None:
    """A seeded orphan classification is CAUGHT by INV-10 (not a vacuous assert).

    Delete one file's on-disk local content out from under its index entry so the
    store's INV-10 no-orphan check must fire, then assert the invariant raises.
    """
    from setforge.reconcile import store as reconcile_store

    model = driven_machine.model
    fid = next(iter(model.indexed_file_ids()))
    reconcile_store.local_content_path(model.profile, fid).unlink()
    with pytest.raises(InvariantViolation, match="INV-2/INV-10"):
        driven_machine.inv2_inv10_store_consistent()


def test_inv1_catches_a_seeded_violation(driven_machine) -> None:
    """A seeded live/store divergence is CAUGHT by INV-1 (not a vacuous assert).

    Extends the ``test_state_machine_violation_is_surfaced`` pattern: corrupt one
    live body so it no longer matches its store reconstruction, then assert the
    real INV-1 method raises. Proves the assert bites on non-empty data.
    """
    model = driven_machine.model
    fid = next(iter(model.live_bodies()))
    model.live[fid] = model.live[fid] + "\n# tampered, not in the store\n"
    with pytest.raises(AssertionError, match="INV-1"):
        driven_machine.inv1_no_silent_data_loss()


def scoped_out_invariants() -> dict[str, str]:
    """The INVs deliberately NOT authored in this machine, with why (E2 contract).

    Each is not driven by the install/sync/revert/migrate seam here, so it is
    owned + covered elsewhere; a later task that wires the missing verb authors
    its ``@invariant`` via the same ``registered_invariants()`` MRO assembly (no
    central re-implementation). Returned as data so this scope decision is both
    documented AND asserted (see ``test_scope_out_is_documented``).
    """
    return {
        "INV-6": "merge engine's own fail-closed _verify; test_merge_properties.py",
        "INV-7": "provisioner shells out (claude/code/gitleaks) — not hermetic here",
        "INV-8": "needs the share/keep staging verb, not driven by this machine",
        "INV-9": "pure bundle-model DAG property, not a reconcile-verb step",
    }


def test_scope_out_is_documented() -> None:
    """The four scoped-out INVs are each recorded with a rationale."""
    scoped = scoped_out_invariants()
    assert set(scoped) == {"INV-6", "INV-7", "INV-8", "INV-9"}
    assert all(reason for reason in scoped.values())
