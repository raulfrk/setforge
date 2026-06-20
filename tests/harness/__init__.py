"""Stateful-test harness scaffold for setforge (RFC 0001 §6, task E1).

This package is the FOUNDATION for the invariant catalog (INV-1..10).
It ships three composable pieces and one fixture seam:

- :mod:`tests.harness.strategies` — Hypothesis strategies that generate
  valid-ish ``setforge.yaml`` config inputs (profiles, tracked-file
  entries, dispositions) plus the per-step command sequence the state
  machine drives.
- :mod:`tests.harness.invariants` — the invariant-helper API: an
  :class:`~tests.harness.invariants.Invariant` protocol, an
  :func:`~tests.harness.invariants.invariant` registration decorator, and
  an :class:`~tests.harness.invariants.InvariantStateMachine` base class
  that asserts every registered invariant after each ``@rule`` step.
- :mod:`tests.harness.model` — :class:`~tests.harness.model.StubReconcileModel`,
  the thin seam the state machine drives. It mimics the install / sync /
  revert / migrate transitions against a tmp_path-isolated store
  (``base/`` + ``local/`` + ``index/`` per RFC §9.3) and a transition
  snapshot stack. The REAL Epic-A reconcile engine replaces the stub's
  ``_engine_*`` methods later (task E2); the verbs, store layout, and
  invariant hook points stay identical so the swap is mechanical.

The harness is RUNNABLE and meta-tested TODAY (see
``tests/harness/test_harness_meta.py``): the state machine drives the
stub over generated sequences, the invariant helper catches a
deliberately-violated toy invariant, and the fixtures isolate the fs +
intercept the subprocess boundary.

EXTENSION POINTS for later per-component tasks are marked inline with
``EXTENSION POINT (E2/...)`` comments — that is where the real
invariants and the real engine plug in.
"""

from __future__ import annotations
