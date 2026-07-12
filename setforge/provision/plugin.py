"""The plugin :class:`Provisioner`: 3-state reconcile via the ``claude`` CLI.

``probe`` memoizes the enabled/disabled split so ``plan`` stays pure — a
disabled plugin plans as an activation, an absent one as an install.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from setforge import claude_plugins
from setforge.binaries import stderr_of
from setforge.errors import PluginToolMissing, ProvisionItemFailed
from setforge.provision.protocol import (
    Identity,
    Outcome,
    ProvisionDelta,
    Provisioner,
    ProvisionItem,
    ProvisionOutcome,
)
from setforge.provision.registry import register

__all__ = ["PluginProvisioner"]


@register("plugin")
class PluginProvisioner(Provisioner):
    type = "plugin"

    def __init__(self) -> None:
        self._enabled: set[Identity] = set()
        self._disabled: set[Identity] = set()

    def probe(self) -> set[Identity]:
        try:
            installed = claude_plugins.list_installed()
        except PluginToolMissing:
            self._enabled = set()
            self._disabled = set()
            return set()
        self._enabled = {
            Identity(key=pid, display=pid)
            for pid, entry in installed.items()
            if entry.get("enabled", True)
        }
        self._disabled = {
            Identity(key=pid, display=pid)
            for pid, entry in installed.items()
            if not entry.get("enabled", True)
        }
        return self._enabled | self._disabled

    def plan(
        self, items: Sequence[ProvisionItem], installed: set[Identity]
    ) -> ProvisionDelta:
        present = {i.key for i in installed}
        disabled = {i.key for i in self._disabled}
        return ProvisionDelta(
            installed=tuple(
                item.identity for item in items if item.identity.key not in present
            ),
            activated=tuple(
                item.identity for item in items if item.identity.key in disabled
            ),
        )

    def apply_one(self, item: ProvisionItem) -> ProvisionOutcome:
        name, marketplace = claude_plugins._split_id(item.identity.display)
        try:
            if item.identity not in self._disabled:
                claude_plugins.plugin_install(name, marketplace)
            claude_plugins.plugin_enable(item.identity.display)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            summary = stderr_of(exc)
            raise ProvisionItemFailed(
                item_id=item.identity.display,
                error_summary=summary,
                full_stderr=summary,
                kind=Outcome.HARD,
            ) from exc
        return ProvisionOutcome(item=item, outcome=Outcome.OK)

    def uninstall_one(self, identity: Identity) -> None:
        claude_plugins.plugin_disable(identity.display)
