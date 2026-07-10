"""Uniform package-provisioning protocol (Epic B foundation).

Every ecosystem provisioner (cargo, python, go, github_release, …) implements
the single :class:`~setforge.provision.protocol.Provisioner` ABC so the driver
owns the cross-cutting invariants (exit-gating, REPORT-no-write, idempotent
skip) once instead of once per ecosystem.
"""

from setforge.provision.driver import exit_code, reconcile
from setforge.provision.protocol import (
    DesiredState,
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
    ReconcileResult,
)
from setforge.provision.receipt import ReceiptStore
from setforge.provision.reference import InMemoryProvisioner
from setforge.provision.registry import build, register

__all__ = [
    "DesiredState",
    "Identity",
    "InMemoryProvisioner",
    "Outcome",
    "ProvisionDelta",
    "ProvisionItem",
    "ProvisionOutcome",
    "Provisioner",
    "ReceiptStore",
    "ReconcileResult",
    "build",
    "exit_code",
    "reconcile",
    "register",
]
