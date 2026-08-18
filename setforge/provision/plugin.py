"""The plugin :class:`Provisioner`: 3-state reconcile via ``claude``."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from setforge import claude_marketplace_cache, claude_plugins
from setforge.binaries import stderr_of
from setforge.errors import (
    MarketplaceCacheMiss,
    PluginToolMissing,
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

__all__ = ["PluginProvisioner"]


@register("plugin")
class PluginProvisioner(Provisioner):
    type = "plugin"

    def __init__(
        self,
        *,
        checkouts: dict[str, tuple[Path, str]] | None = None,
        installed_snapshot: dict[str, dict[str, object]] | None = None,
    ) -> None:
        # checkouts: plugin id -> (cache_dir, sha), for LOCKED LOCAL_CLONE plugins.
        self._enabled: set[Identity] = set()
        self._disabled: set[Identity] = set()
        self._checkouts: dict[str, tuple[Path, str]] = dict(checkouts or {})
        self._installed_snapshot = (
            {name: dict(entry) for name, entry in installed_snapshot.items()}
            if installed_snapshot is not None
            else None
        )

    def probe(self) -> set[Identity]:
        try:
            installed = (
                self._installed_snapshot
                if self._installed_snapshot is not None
                else claude_plugins.list_installed()
            )
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
        # `installed` (set[Identity]) encodes presence only, not enabled-state,
        # so the activation set is derived from the disabled subset memoized by
        # probe() — plugin is the one provisioner with an absent→disabled→active
        # lifecycle. This couples plan() to a preceding probe() on this same
        # instance (the driver contract); see Provisioner.plan's docstring.
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
        try:
            name, marketplace = item.identity.display.split("@", 1)
            if item.identity not in self._disabled:
                self._pin_marketplace(item.identity.display)
                claude_plugins.plugin_install(name, marketplace)
            claude_plugins.plugin_enable(item.identity.display)
        except ValueError as exc:
            summary = f"malformed plugin id {item.identity.display!r}: {exc}"
            raise ProvisionItemFailed(
                item_id=item.identity.display,
                error_summary=summary,
                full_stderr=summary,
                kind=Outcome.HARD,
            ) from exc
        except MarketplaceCacheMiss as exc:
            # Fail closed as HARD rather than falling through to an unpinned install.
            summary = str(exc)
            raise ProvisionItemFailed(
                item_id=item.identity.display,
                error_summary=summary,
                full_stderr=summary,
                kind=Outcome.HARD,
            ) from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            summary = stderr_of(exc)
            raise ProvisionItemFailed(
                item_id=item.identity.display,
                error_summary=summary,
                full_stderr=summary,
                kind=Outcome.HARD,
            ) from exc
        return ProvisionOutcome(item=item, outcome=Outcome.OK)

    def _pin_marketplace(self, plugin_id: str) -> None:
        # RESIDUAL: Claude Code has no hook to disable per-plugin auto-update, so a
        # later background auto-update can still re-pull past this pin (verified: no
        # autoUpdate field/flag; DISABLE_AUTOUPDATER only gates the CLI self-updater).
        target = self._checkouts.get(plugin_id)
        if target is None:
            return
        cache_dir, sha = target
        claude_marketplace_cache.checkout_marketplace_at(cache_dir, sha)

    def uninstall_one(self, identity: Identity) -> None:
        claude_plugins.plugin_disable(identity.display)
