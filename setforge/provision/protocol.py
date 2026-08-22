"""The uniform provisioner protocol surface (spec §2).

Value objects + the :class:`Provisioner` ABC that every ecosystem
provisioner (cargo, python, go, github_release, …) implements. The
plan/apply split is the spine: :meth:`Provisioner.plan` is pure and returns
a typed :class:`ProvisionDelta`; the driver calls :meth:`Provisioner.apply_one`
only when policy is not REPORT (or a report-only run), which makes
REPORT-no-writes structural.
"""

from abc import ABC, abstractmethod
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel


class Outcome(StrEnum):
    """The result of attempting one declared item."""

    OK = "ok"
    SKIP = "skip"  # already present (idempotent no-op) — writes nothing
    SOFT = "soft"  # couldn't attempt (no toolchain/sudo) — warns, does NOT gate exit
    HARD = "hard"  # attempted and failed (install/checksum error) — gates exit


class DesiredState(StrEnum):
    """The ``{present → active}`` lifecycle a declared item targets."""

    ABSENT = "absent"
    PRESENT = "present"
    ACTIVE = "active"


class ObservationOrigin(StrEnum):
    """How SetForge learned that a package is present."""

    EXTERNAL = "external"
    LEGACY_RECEIPT = "legacy-receipt"
    CURRENT_RECEIPT = "current-receipt"


@dataclass(slots=True, frozen=True)
class Identity:
    """A provisioner match key.

    ``key`` is the NORMALIZED match key: equality and hashing derive from it
    alone, so two identities with the same key but a different ``display``
    are equal and hash-equal. ``display`` (excluded from comparison via
    ``compare=False``) carries the ORIGINAL form the subprocess is invoked
    with — normalization must never lose the caller's casing/prefix.
    """

    key: str
    display: str = field(compare=False)


class _EmptyConfig(BaseModel):
    """The default per-item config: a validated model holding nothing.

    Real provisioners supply an ecosystem-specific ``BaseModel`` subclass;
    the base item never holds an opaque dict (a validated model is always
    present so callers cannot smuggle unvalidated shape through ``config``).
    """


@dataclass(slots=True, frozen=True)
class ProvisionItem:
    """One declared thing to provision."""

    type: str
    identity: Identity
    desired: DesiredState = DesiredState.ACTIVE
    version: str | None = None  # resolved ecosystem pin
    checksum: str | None = None
    artifact: str | None = None
    platform: str | None = None
    config: BaseModel = field(default_factory=_EmptyConfig)


@dataclass(slots=True, frozen=True)
class PackageObservation:
    """Frozen provider evidence for one present package."""

    identity: Identity
    origin: ObservationOrigin
    version: str | None = None
    source: str | None = None
    locator: str | None = None
    fingerprint: str | None = None
    checksum: str | None = None
    artifact: str | None = None
    platform: str | None = None


@dataclass(slots=True, frozen=True)
class ProvisionOutcome:
    """The recorded result of applying (or skipping) one item."""

    item: ProvisionItem
    outcome: Outcome
    detail: str = ""


@dataclass(slots=True, frozen=True)
class ProvisionDelta:
    """The success-only delta an install produced (drives revert)."""

    installed: tuple[Identity, ...] = ()
    activated: tuple[Identity, ...] = ()

    def is_empty(self) -> bool:
        """Return True when nothing was installed or activated."""
        return not (self.installed or self.activated)


@dataclass(slots=True, frozen=True)
class ReconcileResult:
    """The full outcome of one reconcile run."""

    delta: ProvisionDelta
    outcomes: tuple[ProvisionOutcome, ...] = ()
    reported: bool = False


class Provisioner(ABC):
    """The uniform contract every ecosystem provisioner implements."""

    type: ClassVar[str]

    @abstractmethod
    def probe(self) -> set[Identity]:
        """Return the currently-installed identities (read-only).

        Backed by a live list OR the receipt store for list-less ecosystems.
        Never writes.
        """

    def inventory_fingerprint(self, installed: set[Identity]) -> Hashable:
        """Return the inventory state that a frozen plan must revalidate.

        Identity-only provisioners inherit the default. Ecosystems whose pins
        depend on richer probe metadata may override it after :meth:`probe`.
        """
        return frozenset(installed)

    def observations(self, installed: set[Identity]) -> tuple[PackageObservation, ...]:
        """Describe present packages without implying ownership."""
        return tuple(
            PackageObservation(identity, ObservationOrigin.EXTERNAL)
            for identity in sorted(installed, key=lambda value: value.key)
        )

    def plan_fingerprint(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> Hashable:
        """Return all external read state that freezes a reconcile plan."""
        del items
        return self.inventory_fingerprint(installed)

    @abstractmethod
    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        """Compute the delta to reach the declared state.

        PURE in the effect sense: no writes and no mutating subprocess, so the
        driver may call this under REPORT without side effects. It is NOT
        required to be a pure function of ``(items, installed)`` alone — an
        implementation MAY read instance state established by a preceding
        :meth:`probe` (e.g. plugin activation derives from the disabled subset,
        which ``installed`` cannot encode). The driver contract therefore calls
        :meth:`plan` on the SAME instance immediately after :meth:`probe`; do
        not reuse a plan across instances or a replayed ``installed`` set.
        """

    @abstractmethod
    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        """Provision one item. The ONLY writing method in the protocol."""

    @abstractmethod
    def uninstall_one(self, identity: Identity) -> None:
        """Undo one install for this ecosystem. Currently an UNWIRED seam.

        No reconcile, revert, or cleanup path calls this today. Actual removal
        is centralized in receipt-based cleanup
        (``cli.cleanup.delete_provisioned`` -> ``_confined_unlink``), which
        unlinks the installed binary recorded in the ``ReceiptStore``. Wiring
        this method into a future revert path would require a SAFE-9 audit of
        the removal.
        """
