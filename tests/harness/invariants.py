"""Invariant-helper API for the stateful harness (RFC 0001 §6, E1).

The contract a future per-component task uses to register an invariant
(one of INV-1..10) and have it asserted after every state-machine step:

    class MergeMachine(InvariantStateMachine):
        model_factory = staticmethod(StubReconcileModel.create)

        @rule()
        def install(self):
            self.model.install()

        @invariant()            # INV-2: base round-trips
        def base_round_trips(self):
            for fid, body in self.model.live_bodies().items():
                assert self.model.base_plus_local(fid) == body

Two ways to run the invariants:

- Inside a :class:`hypothesis.stateful.RuleBasedStateMachine`: every
  ``@invariant()``-decorated method is BOTH a registered harness
  invariant AND a native Hypothesis ``@invariant`` (it is the same
  decorator from ``hypothesis.stateful``), so the stateful driver checks
  it after each rule automatically.
- Standalone, for a focused non-Hypothesis test:
  :meth:`InvariantStateMachine.assert_invariants` runs every registered
  invariant once and raises :class:`InvariantViolation` on the first
  failure — used by the meta-tests to prove the helper actually catches a
  broken invariant.

This module owns NO setforge behavior. It is pure test infrastructure.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from hypothesis.errors import InvalidDefinition
from hypothesis.stateful import RuleBasedStateMachine
from hypothesis.stateful import invariant as _hypothesis_invariant

if TYPE_CHECKING:
    from tests.harness.model import StubReconcileModel

# The env var the real engine's ``transitions.state_root`` honors. Set
# per-example to ``self.model.state_dir`` so every real store / merge / migrate
# write lands under ``self.root`` and never the dev-host state dir.
_STATE_ENV = "SETFORGE_STATE_DIR"

# Sentinel attribute stamped on every @invariant-decorated method so the
# base class can discover them by introspection. Keeping it a private
# string constant (not a public marker) avoids colliding with Hypothesis's
# own internal ``INVARIANT_MARKER``.
_HARNESS_INVARIANT_FLAG = "_setforge_harness_invariant"


class InvariantViolation(AssertionError):
    """Raised when a registered invariant fails.

    Subclasses :class:`AssertionError` so it is a drop-in failure for
    both pytest and Hypothesis's shrinker. The message always names the
    violated invariant so a falsifying example points straight at the
    broken INV.
    """


@runtime_checkable
class Invariant(Protocol):
    """A registered invariant: a bound method asserting a property.

    The method takes no arguments beyond ``self`` and raises (any
    ``AssertionError``) when the property does not hold for the current
    model state. The decorator stamps :data:`_HARNESS_INVARIANT_FLAG` on
    it for discovery.
    """

    __name__: str

    def __call__(self) -> None: ...


def invariant() -> Callable[[Callable[[Any], None]], Callable[[Any], None]]:
    """Mark a state-machine method as a harness invariant.

    The decorated method is registered for discovery via
    :meth:`InvariantStateMachine.registered_invariants` AND wrapped with
    Hypothesis's native ``@invariant`` so the stateful driver checks it
    after every rule. Call it with parentheses (``@invariant()``) to keep
    room for future options (e.g. ``@invariant(id="INV-2")``) without a
    signature break.
    """

    def decorate(fn: Callable[[Any], None]) -> Callable[[Any], None]:
        @functools.wraps(fn)
        def checked(self: Any) -> None:
            # Re-raise the underlying assertion as InvariantViolation so a
            # failure is self-identifying whether it fires via the standalone
            # assert_invariants() path OR inside run_state_machine_as_test
            # (Hypothesis calls this wrapper directly after each rule).
            try:
                fn(self)
            except InvariantViolation:
                raise
            except AssertionError as exc:
                raise InvariantViolation(
                    f"invariant {fn.__name__!r} violated: {exc}"
                ) from exc

        setattr(checked, _HARNESS_INVARIANT_FLAG, True)
        # Layer Hypothesis's own invariant marker on top so the same method
        # fires inside run_state_machine_as_test. The wrapper stays
        # attribute-introspectable for registered_invariants.
        wrapped = _hypothesis_invariant()(checked)
        setattr(wrapped, _HARNESS_INVARIANT_FLAG, True)
        return wrapped

    return decorate


class InvariantStateMachine(RuleBasedStateMachine):
    """Base class for setforge invariant state machines.

    Subclasses supply:

    - ``model_factory`` — a ``staticmethod`` taking the isolation root
      (a ``Path``) and returning a fresh model (the seam the rules
      drive). Defaults to the stub model in normal use.
    - ``@rule`` methods that call the model's verbs.
    - ``@invariant()`` methods (this module's decorator) asserting the
      INV-1..10 properties.

    The machine owns the isolation root and the model instance. Under
    Hypothesis it is constructed with no args (the driver calls
    ``__init__()``); the base then mints its own ``tmp_path``-style root
    via :func:`tempfile.mkdtemp` so each example is fs-isolated even
    outside a pytest ``tmp_path`` fixture. A test may pass an explicit
    root to pin the location (the meta-tests do this with ``tmp_path``).
    """

    #: Set by subclasses to a ``staticmethod`` ``(Path) -> model``.
    model_factory: staticmethod[[Path], StubReconcileModel]

    def __init__(self, root: Path | None = None) -> None:
        # Hypothesis's RuleBasedStateMachine.__init__ requires >= 1 @rule.
        # A machine used ONLY for the standalone assert_invariants() path
        # (no rules, just invariants) is still a legitimate use of this
        # base — so tolerate the "defines no rules" InvalidDefinition and
        # fall back to a minimal init. A machine with rules initializes
        # the full Hypothesis stateful surface as normal.
        try:
            super().__init__()
        except InvalidDefinition:
            self.rules = ()
            self.invariants = ()
        if root is None:
            root = Path(tempfile.mkdtemp(prefix="setforge_harness_"))
        self.root: Path = root
        self.model: StubReconcileModel = type(self).model_factory(root)
        # A machine with @rule methods is one Hypothesis actually DRIVES over a
        # generated sequence; a rule-less machine is only ever used via the
        # standalone assert_invariants() path (which the autouse conftest
        # SETFORGE_STATE_DIR fixture already isolates and which never shells
        # out). Only the driven machine installs the per-example env redirect +
        # subprocess guard, and only it reliably reaches teardown() — so the
        # global-state mutation can never leak from a construct-and-abandon
        # standalone test.
        self._guarded = bool(self.rules)
        if self._guarded:
            self._prev_state_env = os.environ.get(_STATE_ENV)
            os.environ[_STATE_ENV] = str(self.model.state_dir)
            self._install_subprocess_guard()

    def _install_subprocess_guard(self) -> None:
        """Replace ``subprocess.run`` / ``Popen`` with raisers for this example.

        The wire-ready reconcile path (store + merge + migrate) is
        subprocess-free, so any shell-out is an unexpected escape — it raises
        loudly rather than hitting the host ``claude`` / ``code`` / ``gitleaks``.
        Restored in :meth:`teardown` so the guard never leaks across examples.
        """
        self._orig_run = subprocess.run
        self._orig_popen = subprocess.Popen

        def _blocked(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                f"unregistered subprocess call in the invariant machine: "
                f"{args!r} {kwargs!r}"
            )

        subprocess.run = _blocked
        subprocess.Popen = _blocked  # type: ignore[assignment,misc]

    def teardown(self) -> None:
        """Per-example cleanup — Hypothesis calls this after every example.

        Restores the subprocess guard + state-dir env (only if this machine
        installed them) and removes the isolation root so no per-example state
        (store bytes, lockfiles, ``setforge.yaml``) bleeds into the next
        generated sequence.
        """
        if self._guarded:
            subprocess.run = self._orig_run
            subprocess.Popen = self._orig_popen  # type: ignore[misc]
            if self._prev_state_env is None:
                os.environ.pop(_STATE_ENV, None)
            else:
                os.environ[_STATE_ENV] = self._prev_state_env
        shutil.rmtree(self.root, ignore_errors=True)

    @classmethod
    def registered_invariants(cls) -> list[Callable[[Any], None]]:
        """Return every ``@invariant()``-marked method on this class (+ bases).

        Walks the MRO so a subclass inherits its parents' invariants.
        Deterministic order (alphabetical by name) so a multi-invariant
        failure report is stable across runs.
        """
        found: dict[str, Callable[[Any], None]] = {}
        for klass in cls.__mro__:
            for name, attr in vars(klass).items():
                if name in found:
                    continue
                if getattr(attr, _HARNESS_INVARIANT_FLAG, False):
                    found[name] = attr
        return [found[name] for name in sorted(found)]

    def assert_invariants(self) -> None:
        """Run every registered invariant once; raise on the first failure.

        The standalone counterpart to the Hypothesis-driven check. Used by
        focused tests (and the meta-tests) that drive the model by hand
        and then assert the catalog holds. Each registered method is the
        wrapper minted by :func:`invariant`, which already re-raises a
        failed assertion as :class:`InvariantViolation` naming the
        offending invariant.
        """
        for inv in self.registered_invariants():
            inv(self)
