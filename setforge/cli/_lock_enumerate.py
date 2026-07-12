"""The LOCKABLE-surface enumeration shared by ``lock`` and ``install``.

Holds :class:`_LockItem` and :func:`enumerate_lock_items` — the single
definition of *which* declared packages a profile can pin. Both the ``lock``
verb (which resolves each item) and ``install`` (whose ``--locked`` check and
lock override run against exactly this surface) import from here.

This module deliberately imports NONE of the CLI wiring (no ``setforge.cli.app``,
no resolver-registry side effects). Keeping the enumeration free of the ``app``
import means importing ``install`` never pulls in ``cli.lock`` — so the ``lock``
command's ``@app.command`` decorator does not fire ahead of ``install``'s own,
preserving the CLI's registration order.
"""

from __future__ import annotations

from dataclasses import dataclass

from setforge.config import (
    CargoPackage,
    Config,
    GitHubReleasePackage,
    GoPackage,
    LocalPackage,
    PythonPackage,
    ResolvedProfile,
)
from setforge.errors import ConfigError
from setforge.provision.resolve.extension import ExtensionResolveItem
from setforge.provision.resolve.plugin import PluginResolveItem, marketplace_git_url
from setforge.provision.resolve.protocol import PackageType


@dataclass(frozen=True, slots=True)
class _LockItem:
    """One lockable unit: the resolver key + the already-built resolver input.

    ``pkg_type`` selects the resolver via ``get_resolver``; ``resolve_input``
    is exactly what that resolver's ``resolve`` expects (a Package model, a
    :class:`PluginResolveItem`, or an :class:`ExtensionResolveItem`).
    """

    pkg_type: PackageType
    resolve_input: object

    def lock_key(self) -> str:
        """The pin key this item WILL resolve to — known without a network call.

        Every resolver derives its pin ``key`` deterministically from the input
        (cargo→``crate``, python→``package``, go→``module``, github_release→
        ``repo``, plugin/extension→the item's own ``key``), so ``lock --update``
        can select the target BEFORE resolving — matching this against the CLI
        key resolves only the one item, never the packages enumerated before it.
        """
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
    """Return every LOCKABLE item the resolved profile declares.

    Three surfaces (bundles are intentionally out of scope for this verb):

    - ``resolved.packages`` — name refs into ``cfg.packages`` (typed Package
      models). ``local`` packages resolve nothing upstream, so they are SKIPPED
      (no lock entry). Each other Package model is passed to its resolver
      verbatim.
    - ``resolved.claude_plugins`` — bare plugin names resolved through
      ``cfg.claude_plugins`` → ``cfg.marketplaces`` into a
      :class:`PluginResolveItem` (lock key ``name@marketplace``, git URL derived
      once via :func:`marketplace_git_url`).
    - ``resolved.extensions.include`` — ``publisher.name`` ids wrapped in an
      :class:`ExtensionResolveItem`.

    A plugin naming an undeclared plugin / marketplace raises
    :class:`~setforge.errors.ConfigError` — a lockable package with no way to
    resolve is an error, never a silent skip.
    """
    items: list[_LockItem] = []

    for ref in resolved.packages:
        pkg = cfg.packages[ref]
        if isinstance(pkg, LocalPackage):
            continue  # not lockable — nothing upstream to pin
        items.append(_LockItem(PackageType(pkg.type.value), pkg))

    for bare_name in resolved.claude_plugins:
        items.append(_LockItem(PackageType.PLUGIN, _plugin_item(cfg, bare_name)))

    for ext_id in resolved.extensions.include:
        items.append(_LockItem(PackageType.EXTENSION, ExtensionResolveItem(key=ext_id)))

    return items


def _plugin_item(cfg: Config, bare_name: str) -> PluginResolveItem:
    """Build the :class:`PluginResolveItem` for one bare profile plugin name.

    Resolves ``bare_name`` → its marketplace via ``cfg.claude_plugins``, then the
    marketplace's git URL via ``cfg.marketplaces`` + :func:`marketplace_git_url`.
    The lock key is ``name@marketplace`` (matching the plugin id shape used
    elsewhere). An undeclared plugin or an unknown marketplace is a
    :class:`~setforge.errors.ConfigError`. ``load_config`` cross-checks the
    plugin NAME against the top-level ``claude_plugins:`` registry, but its
    marketplace-reference check runs only inside ``apply_local_overlay`` — which
    the lock path never calls — so the ``.get(ref.marketplace)`` guard below is
    the SOLE marketplace-existence check on this path, not defensive redundancy.
    """
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
