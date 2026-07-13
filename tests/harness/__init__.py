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
  the seam the state machine drives. It runs the REAL install / sync /
  revert / migrate reconcile engine (:mod:`setforge.reconcile_apply`,
  :mod:`setforge.reconcile.store`, :mod:`setforge.migrations`,
  :mod:`setforge.transitions`) against a ``self.root``-isolated store
  (``base/`` + ``local/`` + ``index/`` per RFC §9.3) plus a transition
  snapshot stack. The real path is subprocess-free (pure merge, fsync store
  writes, ruamel migration edits); the ``StubReconcileModel`` name is retained
  for continuity with the scaffold's importers.

The harness is RUNNABLE and meta-tested (see
``tests/harness/test_harness_meta.py``): the state machine drives the real
engine over generated sequences, the invariant helper catches a
deliberately-violated toy invariant, and per-example isolation (a
``SETFORGE_STATE_DIR`` redirect + subprocess guard + ``teardown`` rmtree) keeps
each example hermetic. The real INV-1..10 catalog lives in
``tests/invariants/`` (task E2), assembled via the same ``@invariant()``
registration MRO walk.
"""

from __future__ import annotations
