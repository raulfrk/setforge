"""Projects the package + reconcile-block surface into the plugin/extension/cargo
lists the provisioning engines consume.

Reads plugin/extension/cargo intent from ``resolved.packages`` (keyed through
``cfg.packages``) and reconcile policy from ``resolved.reconcile``. Imports
FROM ``config`` at module level; ``config.py`` imports back only via
function-local imports (dodges an import-time cycle)."""

from setforge.config import (
    CargoPackage,
    Config,
    ExtensionPackage,
    Extensions,
    PluginPackage,
    ReconcilePolicy,
    ResolvedProfile,
)
from setforge.errors import ConfigError


def plugin_bare_names(cfg: Config, resolved: ResolvedProfile) -> list[str]:
    return [
        pkg.plugin
        for ref in resolved.packages
        if isinstance(pkg := cfg.packages.get(ref), PluginPackage)
    ]


def plugin_ids(cfg: Config, resolved: ResolvedProfile) -> set[str]:
    # Resolves bare plugin names to "name@marketplace" ids; the sole source
    # of the undeclared-plugin ConfigError text feeding the plugins engine.
    declared: set[str] = set()
    for bare_name in plugin_bare_names(cfg, resolved):
        ref = cfg.claude_plugins.get(bare_name)
        if ref is None:
            raise ConfigError(
                f"profile references undeclared plugin: {bare_name!r} "
                f"(add it to top-level claude_plugins:)"
            )
        declared.add(f"{bare_name}@{ref.marketplace}")
    return declared


def plugin_policy(resolved: ResolvedProfile) -> ReconcilePolicy:
    return resolved.reconcile.plugins.policy


def extensions_input(cfg: Config, resolved: ResolvedProfile) -> Extensions:
    include = [
        pkg.extension
        for ref in resolved.packages
        if isinstance(pkg := cfg.packages.get(ref), ExtensionPackage)
    ]
    exclude = list(resolved.reconcile.extensions.exclude)
    reconcile = resolved.reconcile.extensions.policy
    return Extensions(include=include, exclude=exclude, reconcile=reconcile)


def cargo_crates(cfg: Config, resolved: ResolvedProfile) -> list[str]:
    return [
        pkg.crate.strip()
        for ref in resolved.packages
        if isinstance(pkg := cfg.packages.get(ref), CargoPackage)
        if pkg.crate.strip()
    ]
