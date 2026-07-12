"""The extension :class:`Provisioner`: reconcile VSCode extensions via ``code``."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from setforge import vscode_extensions
from setforge.errors import (
    ExtensionInstallFailed,
    ExtensionToolMissing,
    ProvisionItemFailed,
)
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
)
from setforge.provision.registry import register

__all__ = ["ExtensionProvisioner"]


@register("extension")
class ExtensionProvisioner(Provisioner):
    type = "extension"

    def probe(self) -> set[Identity]:
        try:
            installed = vscode_extensions.list_installed()
        except ExtensionToolMissing:
            return set()
        return {Identity(key=e.casefold(), display=e) for e in installed}

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        present = {i.key for i in installed}
        return ProvisionDelta(
            installed=tuple(
                item.identity for item in items if item.identity.key not in present
            )
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        try:
            vscode_extensions.install_one(item.identity.display)
        except (
            ExtensionInstallFailed,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise ProvisionItemFailed(
                item_id=item.identity.display,
                error_summary=str(exc),
                full_stderr=str(exc),
                kind=Outcome.HARD,
            ) from exc
        return ProvisionOutcome(item=item, outcome=Outcome.OK)

    def uninstall_one(self, identity: Identity) -> None:
        vscode_extensions.uninstall_one(identity.display)
