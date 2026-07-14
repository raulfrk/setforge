"""Union the OLD reconcile fields with the NEW package + reconcile-block surface.

During the expand window both a pre-W1 config (old ``claude_plugins`` /
``extensions`` / ``cargo_binaries`` / ``plugins_reconcile`` fields) and a
new-surface config (``packages`` + a ``reconcile`` block) must resolve
identically. This module is the single place that unions the two so no
read-site or engine has to know both shapes.

Policy rule ``new if new != ADDITIVE else old``: post-resolve both scalars
always hold a value (ADDITIVE when unset), so we cannot tell an explicit
ADDITIVE from a defaulted one. A pre-W1 config has no reconcile block, so
``new`` is the default ADDITIVE and the rule returns ``old`` — byte-identical
to today; a new-only config has ``old`` defaulted to ADDITIVE, so it returns
``new``.

Import discipline: this module imports FROM config and is imported BY
read-sites; config.py and the engines never import it (no cycle).

Load-time BATCHED validation of package plugin-refs arrives when the
validators are repointed (a later task); until then the adapter raises at
read-time with the single-offender message — the intended expand-window
interim, not a bug.
"""

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
    """Ordered union of old ``claude_plugins`` + plugin-package bare names."""
    return _merge_list(
        resolved.claude_plugins,
        [
            pkg.plugin
            for ref in resolved.packages
            if isinstance(pkg := cfg.packages[ref], PluginPackage)
        ],
    )


def plugin_ids(cfg: Config, resolved: ResolvedProfile) -> set[str]:
    """Resolve the unioned bare names to ``"name@marketplace"`` ids.

    Mirrors ``claude_plugins._declared_plugin_ids`` exactly, including its
    undeclared-plugin ConfigError message, so a pre-W1 config produces a
    byte-identical id set.
    """
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
    """Union old ``plugins_reconcile`` with the new ``reconcile.plugins.policy``."""
    new = resolved.reconcile.plugins.policy
    old = resolved.plugins_reconcile
    return new if new != ReconcilePolicy.ADDITIVE else old


def extensions_input(cfg: Config, resolved: ResolvedProfile) -> Extensions:
    """Union old ``extensions`` with extension packages + the reconcile block."""
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
    """Union old ``cargo_binaries`` with cargo-package crate names."""
    return _merge_list(
        [c.strip() for c in resolved.cargo_binaries if c.strip()],
        [
            pkg.crate.strip()
            for ref in resolved.packages
            if isinstance(pkg := cfg.packages[ref], CargoPackage)
            if pkg.crate.strip()
        ],
    )


def synth_plugin_profile(cfg: Config, resolved: ResolvedProfile) -> ResolvedProfile:
    """Overlay the unioned plugin names + policy onto a copy of ``resolved``.

    Lets the unchanged ``claude_plugins`` reconcile engine (which reads
    ``profile.claude_plugins`` + ``profile.plugins_reconcile``) see the union
    with no engine edit.
    """
    return resolved.model_copy(
        update={
            "claude_plugins": plugin_bare_names(cfg, resolved),
            "plugins_reconcile": plugin_policy(resolved),
        }
    )
