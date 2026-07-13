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
  the seam the state machine drives, now backed by the REAL Epic-A reconcile
  engine (task E2) against a ``self.root``-isolated store (``base/`` +
  ``local/`` + ``index/`` per RFC §9.3) and a transition snapshot stack.

The harness is RUNNABLE and meta-tested (see
``tests/harness/test_harness_meta.py``). The real INV-1..10 catalog lives in
``tests/invariants/`` (task E2).
"""

from __future__ import annotations
