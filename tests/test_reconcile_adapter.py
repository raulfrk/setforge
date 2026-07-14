"""Golden tests for the reconcile adapter.

The adapter unions the OLD-field declarations (``claude_plugins``,
``extensions``, ``cargo_binaries``, ``plugins_reconcile``) with the NEW
package + reconcile-block surface, so a pre-W1 config and a new-surface
config resolve identically during the expand window.
"""

from typing import Any

import pytest

from setforge import reconcile_adapter as adapter
from setforge.claude_plugins import _declared_plugin_ids
from setforge.config import (
    Config,
    ReconcilePolicy,
    ResolvedProfile,
    resolve_profile,
)
from setforge.errors import ConfigError

_MP = {"mp": {"source": "github", "repo": "o/r"}}
_TF = {"t": {"src": "t", "dst": "~/t"}}


def _cfg(profiles: dict[str, Any], **top: Any) -> Config:
    """Build a Config from dicts so defaults + model_fields_set are realistic."""
    base = {
        "tracked_files": _TF,
        "marketplaces": _MP,
        "profiles": profiles,
    }
    base.update(top)
    return Config.model_validate(base)


# --- Test 1: plugin_ids GOLDEN + pre-W1 parity -----------------------------


def test_plugin_ids_old_way_golden_and_declared_parity() -> None:
    cfg = _cfg(
        profiles={"p": {"claude_plugins": ["sp"]}},
        claude_plugins={"sp": {"marketplace": "mp"}},
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.plugin_ids(cfg, resolved) == {"sp@mp"}
    # byte-identity proof against the live engine helper for a pre-W1 config.
    assert adapter.plugin_ids(cfg, resolved) == _declared_plugin_ids(cfg, resolved)


# --- Test 2: plugin_ids NEW surface + union dedup --------------------------


def test_plugin_ids_new_package_surface() -> None:
    cfg = _cfg(
        profiles={"p": {"packages": ["sp"]}},
        claude_plugins={"sp": {"marketplace": "mp"}},
        packages={"sp": {"type": "plugin", "plugin": "sp"}},
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.plugin_ids(cfg, resolved) == {"sp@mp"}


def test_plugin_ids_old_and_new_dedup() -> None:
    cfg = _cfg(
        profiles={"p": {"claude_plugins": ["sp"], "packages": ["sp"]}},
        claude_plugins={"sp": {"marketplace": "mp"}},
        packages={"sp": {"type": "plugin", "plugin": "sp"}},
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.plugin_ids(cfg, resolved) == {"sp@mp"}
    assert adapter.plugin_bare_names(cfg, resolved) == ["sp"]


# --- Test 3: undeclared ConfigError (VERBATIM message snapshot) ------------


def test_plugin_ids_undeclared_raises_verbatim() -> None:
    cfg = _cfg(profiles={"p": {"claude_plugins": ["ghost"]}})
    resolved = resolve_profile(cfg, "p")
    with pytest.raises(ConfigError) as exc:
        adapter.plugin_ids(cfg, resolved)
    assert str(exc.value) == (
        "profile references undeclared plugin: 'ghost' "
        "(add it to top-level claude_plugins:)"
    )


# --- Test 4: extensions_input three-shape ----------------------------------


def _ext_cfg(profile: dict[str, Any], **top: Any) -> tuple[Config, ResolvedProfile]:
    cfg = _cfg(profiles={"p": profile}, **top)
    return cfg, resolve_profile(cfg, "p")


def test_extensions_input_old_shape() -> None:
    cfg, resolved = _ext_cfg({"extensions": {"include": ["A"]}})
    got = adapter.extensions_input(cfg, resolved)
    assert got.include == ["A"]


def test_extensions_input_new_shape() -> None:
    cfg, resolved = _ext_cfg(
        {"packages": ["A"]},
        packages={"A": {"type": "extension", "extension": "A"}},
    )
    got = adapter.extensions_input(cfg, resolved)
    assert got.include == ["A"]


def test_extensions_input_both_dedup() -> None:
    cfg, resolved = _ext_cfg(
        {"extensions": {"include": ["A"]}, "packages": ["A"]},
        packages={"A": {"type": "extension", "extension": "A"}},
    )
    got = adapter.extensions_input(cfg, resolved)
    assert got.include == ["A"]


def test_extensions_input_exclude_union() -> None:
    cfg, resolved = _ext_cfg(
        {
            "extensions": {"include": ["A"], "exclude": ["x"]},
            "reconcile": {"extensions": {"exclude": ["y"]}},
        },
    )
    got = adapter.extensions_input(cfg, resolved)
    assert got.exclude == ["x", "y"]


# --- Test 5: policy table-test (both axes) ---------------------------------


def test_plugin_policy_preserves_prewave_old_via_inheritance() -> None:
    # parent sets old plugins_reconcile=prune, child has no new reconcile block.
    cfg = _cfg(
        profiles={
            "base": {"plugins_reconcile": "prune"},
            "p": {"extends": "base"},
        },
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.plugin_policy(resolved) is ReconcilePolicy.PRUNE


def test_plugin_policy_new_block_only() -> None:
    cfg = _cfg(profiles={"p": {"reconcile": {"plugins": {"policy": "prune"}}}})
    resolved = resolve_profile(cfg, "p")
    assert adapter.plugin_policy(resolved) is ReconcilePolicy.PRUNE


def test_plugin_policy_new_additive_falls_to_old() -> None:
    cfg = _cfg(
        profiles={
            "p": {
                "plugins_reconcile": "prune",
                "reconcile": {"plugins": {"policy": "additive"}},
            }
        },
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.plugin_policy(resolved) is ReconcilePolicy.PRUNE


def test_extension_policy_preserves_prewave_old_via_inheritance() -> None:
    cfg = _cfg(
        profiles={
            "base": {"extensions": {"reconcile": "prune"}},
            "p": {"extends": "base"},
        },
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.extensions_input(cfg, resolved).reconcile is ReconcilePolicy.PRUNE


def test_extension_policy_new_block_only() -> None:
    cfg = _cfg(profiles={"p": {"reconcile": {"extensions": {"policy": "prune"}}}})
    resolved = resolve_profile(cfg, "p")
    assert adapter.extensions_input(cfg, resolved).reconcile is ReconcilePolicy.PRUNE


def test_extension_policy_new_additive_falls_to_old() -> None:
    cfg = _cfg(
        profiles={
            "p": {
                "extensions": {"reconcile": "prune"},
                "reconcile": {"extensions": {"policy": "additive"}},
            }
        },
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.extensions_input(cfg, resolved).reconcile is ReconcilePolicy.PRUNE


# --- Test 6: cargo_crates union --------------------------------------------


def test_cargo_crates_union() -> None:
    cfg = _cfg(
        profiles={"p": {"cargo_binaries": ["rg"], "packages": ["fd"]}},
        packages={"fd": {"type": "cargo", "crate": "fd"}},
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.cargo_crates(cfg, resolved) == ["rg", "fd"]


def test_cargo_crates_dedup_repeat() -> None:
    cfg = _cfg(
        profiles={"p": {"cargo_binaries": ["rg"], "packages": ["rg"]}},
        packages={"rg": {"type": "cargo", "crate": "rg"}},
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.cargo_crates(cfg, resolved) == ["rg"]


# --- Test 7: synth_plugin_profile ------------------------------------------


def test_synth_plugin_profile() -> None:
    cfg = _cfg(
        profiles={
            "p": {
                "claude_plugins": ["sp"],
                "packages": ["extra"],
                "plugins_reconcile": "additive",
                "reconcile": {"plugins": {"policy": "prune"}},
                "tracked_files": ["t"],
            }
        },
        claude_plugins={"sp": {"marketplace": "mp"}, "extra": {"marketplace": "mp"}},
        packages={"extra": {"type": "plugin", "plugin": "extra"}},
    )
    resolved = resolve_profile(cfg, "p")
    synth = adapter.synth_plugin_profile(cfg, resolved)
    assert isinstance(synth, ResolvedProfile)
    assert synth.claude_plugins == adapter.plugin_bare_names(cfg, resolved)
    assert synth.plugins_reconcile == adapter.plugin_policy(resolved)
    # other fields unchanged.
    assert synth.tracked_files == resolved.tracked_files
    assert synth.packages == resolved.packages
    assert synth.reconcile == resolved.reconcile
