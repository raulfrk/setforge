"""The reference provisioner — copy this shape to add a real one (spec §6).

:class:`InMemoryProvisioner` is the worked example every ecosystem provisioner
(cargo, python, go, github_release) replicates. It has no subprocess: installs
mutate an in-memory set OR, in marker mode, write a :class:`ReceiptStore`
receipt. It fully exercises the acceptance — partial HARD → nonzero exit,
REPORT → zero writes, a second identical run → an empty delta (INV-7) — so the
driver contract is proven once here rather than once per ecosystem.

Modeling a *successful install*: the driver gates exit on HARD alone, so any
non-HARD outcome counts as success. A genuine install returns ``Outcome.OK``;
an already-present no-op returns ``Outcome.SKIP`` (writes nothing). Both leave
exit at 0.
"""

from collections.abc import Sequence

from setforge.errors import ProvisionItemFailed
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
)
from setforge.provision.receipt import ReceiptStore
from setforge.provision.registry import register


@register("reference")
class InMemoryProvisioner(Provisioner):
    """A subprocess-free provisioner backing installs with a set or a receipt.

    Args:
        installed: Identities to seed as already-present (in-memory mode).
        fail_hard: Identity keys whose ``apply_one`` raises a HARD failure.
        soft: Identity keys whose ``apply_one`` returns a SOFT outcome and
            installs nothing (the missing-toolchain analog).
        receipts: When given, probe/install go through this
            :class:`ReceiptStore` (marker mode) instead of the in-memory set.
    """

    type = "reference"

    def __init__(
        self,
        *,
        installed: set[Identity] | None = None,
        fail_hard: set[str] | None = None,
        soft: set[str] | None = None,
        receipts: ReceiptStore | None = None,
    ) -> None:
        self._installed = set(installed) if installed else set()
        self._fail_hard = fail_hard or set()
        self._soft = soft or set()
        self._receipts = receipts

    def probe(self) -> set[Identity]:
        """Return the installed identities from the receipt store or the set."""
        if self._receipts is not None:
            return self._receipts.installed()
        return set(self._installed)

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        """Return the declared identities not yet installed. PURE — no writes."""
        return ProvisionDelta(
            installed=tuple(
                item.identity for item in items if item.identity not in installed
            )
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        """Install one item, or raise HARD / return SOFT per the scripted sets."""
        key = item.identity.key
        if key in self._fail_hard:
            raise ProvisionItemFailed(
                item_id=key,
                error_summary="scripted hard failure",
                full_stderr="scripted hard failure",
                kind=Outcome.HARD,
            )
        if key in self._soft:
            return ProvisionOutcome(
                item=item, outcome=Outcome.SOFT, detail="scripted soft skip"
            )
        if item.identity in self.probe():
            return ProvisionOutcome(item=item, outcome=Outcome.SKIP, detail="present")
        self._install(item)
        return ProvisionOutcome(item=item, outcome=Outcome.OK, detail="installed")

    def uninstall_one(self, identity: Identity) -> None:
        """Undo one install — drop the receipt (marker mode) or the set entry."""
        if self._receipts is not None:
            self._receipts.remove(identity)
            return
        self._installed.discard(identity)

    def _install(self, item: ProvisionItem) -> None:
        """Commit the install — the receipt write is the last step (marker mode)."""
        if self._receipts is not None:
            self._receipts.record(
                item.identity,
                version=item.version,
                checksum=item.checksum,
                path=None,
            )
            return
        self._installed.add(item.identity)
