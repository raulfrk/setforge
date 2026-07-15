"""Unions the OLD reconcile fields with the NEW package + reconcile-block surface.

Policy: ``new if new != ADDITIVE else old`` (both always hold a value
post-resolve, so this is the only way to detect an unset ``new``). Imports
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
    _merge_list,
)
from setforge.errors import ConfigError


def plugin_bare_names(cfg: Config, resolved: ResolvedProfile) -> list[str]:
    return _merge_list(
        resolved.claude_plugins,
        [
            pkg.plugin
            for ref in resolved.packages
            if isinstance(pkg := cfg.packages[ref], PluginPackage)
        ],
    )


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
    new = resolved.reconcile.plugins.policy
    old = resolved.plugins_reconcile
    return new if new != ReconcilePolicy.ADDITIVE else old


def extensions_input(cfg: Config, resolved: ResolvedProfile) -> Extensions:
    include = _merge_list(
        resolved.extensions.include,
        [
            pkg.extension
            for ref in resolved.packages
            if isinstance(pkg := cfg.packages[ref], ExtensionPackage)
        ],
    )
    exclude = _merge_list(
        resolved.extensions.exclude, resolved.reconcile.extensions.exclude
    )
    new_pol = resolved.reconcile.extensions.policy
    old_pol = resolved.extensions.reconcile
    reconcile = new_pol if new_pol != ReconcilePolicy.ADDITIVE else old_pol
    return Extensions(include=include, exclude=exclude, reconcile=reconcile)


def cargo_crates(cfg: Config, resolved: ResolvedProfile) -> list[str]:
    return _merge_list(
        [c.strip() for c in resolved.cargo_binaries if c.strip()],
        [
            pkg.crate.strip()
            for ref in resolved.packages
            if isinstance(pkg := cfg.packages[ref], CargoPackage)
            if pkg.crate.strip()
        ],
    )
