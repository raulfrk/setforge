"""The real INV-1..10 invariant machines for setforge (RFC 0001 §6, task E2).

Each module here authors the ``@invariant()`` methods a component owns (per the
``docs/RULES.md`` ownership matrix) as a subclass of
:class:`tests.harness.invariants.InvariantStateMachine`. The harness assembles
them via the ``registered_invariants()`` MRO walk and checks each after every
generated install / sync / revert / migrate step, driving the REAL reconcile
engine (:mod:`tests.harness.model`).
"""

from __future__ import annotations
