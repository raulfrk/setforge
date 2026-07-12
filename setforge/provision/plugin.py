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

    def __init__(self, *, checkouts: dict[str, tuple[Path, str]] | None = None) -> None:
        """Optionally carry pinned-marketplace checkout targets.

        ``checkouts`` maps a ``name@marketplace`` plugin id to its
        ``(cache_dir, sha)`` — present only for a LOCKED plugin under
        LOCAL_CLONE mode (built by :func:`setforge.claude_plugins.reconcile`).
        When :meth:`apply_one` finds a target for the plugin it is installing,
        it hard-resets that marketplace cache to the PINNED commit BEFORE
        ``claude plugin install`` runs, so ``claude`` reads the plugin from the
        pinned commit rather than ``origin/HEAD`` (spec §B3 strong install).
        The registry's :func:`~setforge.provision.registry.build` constructs
        this with no args, so ``checkouts`` defaults to empty — an unpinned
        plugin, a REGULAR-mode install, or a no-lock install all keep today's
        marketplace-id install unchanged.
        """
        self._enabled: set[Identity] = set()
        self._disabled: set[Identity] = set()
        self._checkouts: dict[str, tuple[Path, str]] = checkouts or {}

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
            # A pinned-checkout failure (bad sha, detached-head issue, fetch
            # down) is a clean typed error — surface it as a HARD item failure
            # so nothing partial installs, rather than letting it escape as a
            # traceback or falling through to an unpinned install.
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
        """Hard-reset the plugin's marketplace cache to its pinned commit.

        A no-op unless a ``(cache_dir, sha)`` was threaded in for ``plugin_id``
        (only for a LOCKED plugin under LOCAL_CLONE). When present, the cache is
        reset to the PINNED sha so the ensuing ``claude plugin install`` reads
        the plugin from that commit, not ``origin/HEAD`` — the git
        content-addressing IS the integrity guarantee (spec §B3). A checkout
        failure raises :class:`MarketplaceCacheMiss`, caught by
        :meth:`apply_one` as a HARD failure.

        RESIDUAL (spec §C, "disable plugin autoUpdate on a pinned plugin"):
        Claude Code has NO hook to disable per-plugin auto-update — no
        ``autoUpdate`` config field, no ``--no-auto-update`` flag, and
        ``DISABLE_AUTOUPDATER`` gates only the CLI self-updater, not plugins
        (verified against current claude-code behavior). So this pins the cache
        at install time, but a later background auto-update can still re-pull the
        marketplace past the pin. There is no clean way to prevent that today;
        re-running ``setforge install`` re-pins. Left as a documented residual
        rather than faked.
        """
        target = self._checkouts.get(plugin_id)
        if target is None:
            return
        cache_dir, sha = target
        claude_marketplace_cache.checkout_marketplace_at(cache_dir, sha)

    def uninstall_one(self, identity: Identity) -> None:
        claude_plugins.plugin_disable(identity.display)
