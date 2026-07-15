from typing import Any

import pytest

from setforge import reconcile_adapter as adapter
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
    base = {
        "tracked_files": _TF,
        "marketplaces": _MP,
        "profiles": profiles,
    }
    base.update(top)
    return Config.model_validate(base)


def test_plugin_ids_from_package_surface() -> None:
    cfg = _cfg(
        profiles={"p": {"packages": ["sp"]}},
        claude_plugins={"sp": {"marketplace": "mp"}},
        packages={"sp": {"type": "plugin", "plugin": "sp"}},
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.plugin_ids(cfg, resolved) == {"sp@mp"}
    assert adapter.plugin_bare_names(cfg, resolved) == ["sp"]


def test_plugin_ids_undeclared_via_package_raises_verbatim() -> None:
    cfg = _cfg(
        profiles={"p": {"packages": ["p"]}},
        packages={"p": {"type": "plugin", "plugin": "ghost"}},
    )
    resolved = resolve_profile(cfg, "p")
    with pytest.raises(ConfigError) as exc:
        adapter.plugin_ids(cfg, resolved)
    assert str(exc.value) == (
        "profile references undeclared plugin: 'ghost' "
        "(add it to top-level claude_plugins:)"
    )


def _ext_cfg(profile: dict[str, Any], **top: Any) -> tuple[Config, ResolvedProfile]:
    cfg = _cfg(profiles={"p": profile}, **top)
    return cfg, resolve_profile(cfg, "p")


def test_extensions_input_from_package_surface() -> None:
    cfg, resolved = _ext_cfg(
        {"packages": ["A"]},
        packages={"A": {"type": "extension", "extension": "A"}},
    )
    got = adapter.extensions_input(cfg, resolved)
    assert got.include == ["A"]


def test_extensions_input_exclude_from_reconcile_block() -> None:
    cfg, resolved = _ext_cfg(
        {"reconcile": {"extensions": {"exclude": ["y"]}}},
    )
    got = adapter.extensions_input(cfg, resolved)
    assert got.exclude == ["y"]


def test_extensions_input_exclude_merges_across_chain() -> None:
    cfg = _cfg(
        profiles={
            "base": {"reconcile": {"extensions": {"exclude": ["x"]}}},
            "p": {"extends": "base", "reconcile": {"extensions": {"exclude": ["y"]}}},
        },
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.extensions_input(cfg, resolved).exclude == ["x", "y"]


def test_plugin_policy_from_reconcile_block() -> None:
    cfg = _cfg(profiles={"p": {"reconcile": {"plugins": {"policy": "prune"}}}})
    resolved = resolve_profile(cfg, "p")
    assert adapter.plugin_policy(resolved) is ReconcilePolicy.PRUNE


def test_plugin_policy_inherits_across_chain() -> None:
    cfg = _cfg(
        profiles={
            "base": {"reconcile": {"plugins": {"policy": "prune"}}},
            "p": {"extends": "base"},
        },
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.plugin_policy(resolved) is ReconcilePolicy.PRUNE


def test_extension_policy_from_reconcile_block() -> None:
    cfg = _cfg(profiles={"p": {"reconcile": {"extensions": {"policy": "prune"}}}})
    resolved = resolve_profile(cfg, "p")
    assert adapter.extensions_input(cfg, resolved).reconcile is ReconcilePolicy.PRUNE


def test_extension_policy_inherits_across_chain() -> None:
    cfg = _cfg(
        profiles={
            "base": {"reconcile": {"extensions": {"policy": "prune"}}},
            "p": {"extends": "base"},
        },
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.extensions_input(cfg, resolved).reconcile is ReconcilePolicy.PRUNE


def test_cargo_crates_from_package_surface() -> None:
    cfg = _cfg(
        profiles={"p": {"packages": ["fd"]}},
        packages={"fd": {"type": "cargo", "crate": "fd"}},
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.cargo_crates(cfg, resolved) == ["fd"]


def test_cargo_crates_projects_each_ref_in_order() -> None:
    cfg = _cfg(
        profiles={"p": {"packages": ["rg", "fd"]}},
        packages={
            "rg": {"type": "cargo", "crate": "ripgrep"},
            "fd": {"type": "cargo", "crate": "fd-find"},
        },
    )
    resolved = resolve_profile(cfg, "p")
    assert adapter.cargo_crates(cfg, resolved) == ["ripgrep", "fd-find"]
