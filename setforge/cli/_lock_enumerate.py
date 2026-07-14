"""The LOCKABLE-surface enumeration shared by ``lock`` and ``install`` (no
CLI imports, preserving command registration order)."""

from __future__ import annotations

from dataclasses import dataclass

from setforge import reconcile_adapter
from setforge.config import (
    CargoPackage,
    Config,
    ExtensionPackage,
    GitHubReleasePackage,
    GoPackage,
    LocalPackage,
    PluginPackage,
    PythonPackage,
    ResolvedProfile,
)
from setforge.errors import ConfigError
from setforge.provision.resolve.extension import ExtensionResolveItem
from setforge.provision.resolve.plugin import PluginResolveItem, marketplace_git_url
from setforge.provision.resolve.protocol import PackageType


@dataclass(frozen=True, slots=True)
class _LockItem:
    pkg_type: PackageType
    resolve_input: object

    def lock_key(self) -> str:
        match self.resolve_input:
            case CargoPackage():
                return self.resolve_input.crate
            case PythonPackage():
                return self.resolve_input.package
            case GoPackage():
                return self.resolve_input.module
            case GitHubReleasePackage():
                return self.resolve_input.repo
            case PluginResolveItem() | ExtensionResolveItem():
                return self.resolve_input.key
            case _:  # pragma: no cover - exhaustive over the lockable inputs
                raise AssertionError(
                    f"no lock-key mapping for resolve input {self.resolve_input!r}"
                )


def enumerate_lock_items(cfg: Config, resolved: ResolvedProfile) -> list[_LockItem]:
    # The ONE lockable-surface definition; bundles out of scope.
    items: list[_LockItem] = []

    for ref in resolved.packages:
        pkg = cfg.packages[ref]
        # Plugin/extension packages lock via the adapter loops below (which
        # union them with the OLD fields and yield the resolver-shaped
        # PluginResolveItem / ExtensionResolveItem); enumerating them here too
        # would double-lock AND hand the resolver a raw package it rejects.
        if isinstance(pkg, LocalPackage | PluginPackage | ExtensionPackage):
            continue
        items.append(_LockItem(PackageType(pkg.type.value), pkg))

    for bare_name in reconcile_adapter.plugin_bare_names(cfg, resolved):
        items.append(_LockItem(PackageType.PLUGIN, _plugin_item(cfg, bare_name)))

    for ext_id in reconcile_adapter.extensions_input(cfg, resolved).include:
        items.append(_LockItem(PackageType.EXTENSION, ExtensionResolveItem(key=ext_id)))

    return items


def _plugin_item(cfg: Config, bare_name: str) -> PluginResolveItem:
    # SOLE marketplace check on the lock path (apply_local_overlay's never runs here).
    ref = cfg.claude_plugins.get(bare_name)
    if ref is None:
        raise ConfigError(
            f"profile references undeclared plugin: {bare_name!r} "
            f"(add it to top-level claude_plugins:)"
        )
    mp_source = cfg.marketplaces.get(ref.marketplace)
    if mp_source is None:
        raise ConfigError(
            f"plugin {bare_name!r} references unknown marketplace "
            f"{ref.marketplace!r} (add it to top-level marketplaces:)"
        )
    return PluginResolveItem(
        key=f"{bare_name}@{ref.marketplace}",
        git_url=marketplace_git_url(mp_source),
    )
