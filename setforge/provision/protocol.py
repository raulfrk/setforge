"""The uniform provisioner protocol surface (spec §2).

Value objects + the :class:`Provisioner` ABC that every ecosystem
provisioner (cargo, python, go, github_release, …) implements. The
plan/apply split is the spine: :meth:`Provisioner.plan` is pure and returns
a typed :class:`ProvisionDelta`; the driver calls :meth:`Provisioner.apply_one`
only when policy is not REPORT (or a report-only run), which makes
REPORT-no-writes structural.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
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
    version: str | None = None  # pin; drift is a REPORT signal only (upgrade is later)
    checksum: str | None = None
    config: BaseModel = field(default_factory=_EmptyConfig)


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

    @abstractmethod
    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        """Compute the delta to reach the declared state. PURE.

        No writes and no mutating subprocess — the driver may call this under
        REPORT without side effects.
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
