"""Tests for config schema, YAML loading, and validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from my_setup.config import (
    ClaudePluginRef,
    Config,
    Dotfile,
    Extensions,
    MarketplaceSource,
    MarketplaceSourceKind,
    Profile,
    ReconcilePolicy,
    ResolvedProfile,
    load_config,
    resolve_profile,
)
from my_setup.errors import ConfigError, ProfileNotFound

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_sample_config() -> None:
    cfg = load_config(FIXTURES / "sample_config.yaml")
    assert cfg.version == 1
    assert set(cfg.dotfiles) == {"claude_md", "vscode_settings"}
    assert cfg.dotfiles["claude_md"].preserve_user_sections is True
    assert cfg.dotfiles["vscode_settings"].preserve_user_keys == [
        "editor.fontSize",
        "workbench.colorTheme",
    ]
    assert cfg.dotfiles["vscode_settings"].template is True
    assert set(cfg.profiles) == {"base", "child"}
    assert cfg.profiles["child"].extends == "base"


def test_marketplace_source_kinds() -> None:
    cfg = load_config(FIXTURES / "sample_config.yaml")
    assert cfg.marketplaces["official"].source is MarketplaceSourceKind.GITHUB
    assert cfg.marketplaces["official"].repo == "anthropics/claude-plugins-official"
    assert cfg.marketplaces["local-fork"].source is MarketplaceSourceKind.PATH
    assert cfg.marketplaces["local-fork"].path == Path("~/dev/my-marketplace")


def test_reconcile_policy_parsed_as_enum() -> None:
    cfg = load_config(FIXTURES / "sample_config.yaml")
    assert cfg.profiles["base"].extensions.reconcile is ReconcilePolicy.ADDITIVE
    assert cfg.profiles["child"].extensions.reconcile is ReconcilePolicy.PRUNE


def test_unknown_reconcile_policy_rejected() -> None:
    with pytest.raises(ValidationError):
        Extensions(reconcile="yolo")


def test_marketplace_source_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        MarketplaceSource(source=MarketplaceSourceKind.GITHUB)
    with pytest.raises(ValidationError):
        MarketplaceSource(
            source=MarketplaceSourceKind.GITHUB,
            repo="a/b",
            path=Path("/tmp"),
        )


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_load_config_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load_config(empty)


def test_dotfile_defaults() -> None:
    df = Dotfile(src=Path("a"), dst="b")
    assert df.template is False
    assert df.preserve_user_sections is False
    assert df.preserve_user_keys == []


def test_profile_defaults() -> None:
    p = Profile()
    assert p.extends is None
    assert p.dotfiles == []
    assert p.extensions == Extensions()
    assert p.claude_plugins == []
    assert p.plugins_reconcile is ReconcilePolicy.ADDITIVE
    assert p.bootstrap == []


def test_claude_plugin_ref() -> None:
    ref = ClaudePluginRef(marketplace="official")
    assert ref.marketplace == "official"


def test_config_round_trip_via_model() -> None:
    cfg = load_config(FIXTURES / "sample_config.yaml")
    dumped = cfg.model_dump()
    reloaded = Config.model_validate(dumped)
    assert reloaded == cfg


def _cfg(profiles: dict[str, Profile]) -> Config:
    return Config(
        dotfiles={"d": Dotfile(src=Path("a"), dst="b")},
        profiles=profiles,
    )


def test_resolve_single_profile() -> None:
    cfg = _cfg({"only": Profile(dotfiles=["x", "y"])})
    resolved = resolve_profile(cfg, "only")
    assert isinstance(resolved, ResolvedProfile)
    assert resolved.dotfiles == ["x", "y"]
    assert resolved.extends is None


def test_resolve_two_level_chain_lists_and_scalars() -> None:
    cfg = _cfg(
        {
            "parent": Profile(
                dotfiles=["a", "b"],
                claude_plugins=["p1"],
                extensions=Extensions(include=["e1"], reconcile=ReconcilePolicy.PRUNE),
                plugins_reconcile=ReconcilePolicy.PRUNE,
            ),
            "child": Profile(
                extends="parent",
                dotfiles=["b", "c"],
                claude_plugins=["p2"],
                extensions=Extensions(include=["e2"]),
            ),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.dotfiles == ["a", "b", "c"]
    assert resolved.claude_plugins == ["p1", "p2"]
    assert resolved.extensions.include == ["e1", "e2"]
    assert resolved.extensions.reconcile is ReconcilePolicy.PRUNE
    assert resolved.plugins_reconcile is ReconcilePolicy.PRUNE


def test_resolve_three_level_chain() -> None:
    cfg = _cfg(
        {
            "grand": Profile(dotfiles=["g"]),
            "parent": Profile(extends="grand", dotfiles=["p"]),
            "child": Profile(extends="parent", dotfiles=["c"]),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.dotfiles == ["g", "p", "c"]


def test_resolve_dedup_preserves_first_occurrence() -> None:
    cfg = _cfg(
        {
            "parent": Profile(dotfiles=["a", "b"]),
            "child": Profile(extends="parent", dotfiles=["a", "c", "b"]),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.dotfiles == ["a", "b", "c"]


def test_resolve_scalar_inherits_when_child_unset() -> None:
    cfg = _cfg(
        {
            "parent": Profile(plugins_reconcile=ReconcilePolicy.PRUNE),
            "child": Profile(extends="parent"),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.plugins_reconcile is ReconcilePolicy.PRUNE


def test_resolve_scalar_child_explicit_override() -> None:
    cfg = _cfg(
        {
            "parent": Profile(plugins_reconcile=ReconcilePolicy.PRUNE),
            "child": Profile(
                extends="parent",
                plugins_reconcile=ReconcilePolicy.ADDITIVE,
            ),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.plugins_reconcile is ReconcilePolicy.ADDITIVE


def test_resolve_extension_reconcile_inherits() -> None:
    cfg = _cfg(
        {
            "parent": Profile(extensions=Extensions(reconcile=ReconcilePolicy.PRUNE)),
            "child": Profile(extends="parent", extensions=Extensions(include=["x"])),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.extensions.reconcile is ReconcilePolicy.PRUNE
    assert resolved.extensions.include == ["x"]


def test_resolve_cycle_raises_with_chain() -> None:
    cfg = _cfg(
        {
            "a": Profile(extends="b"),
            "b": Profile(extends="a"),
        }
    )
    with pytest.raises(ConfigError, match="profile cycle") as exc_info:
        resolve_profile(cfg, "a")
    assert "a" in str(exc_info.value)
    assert "b" in str(exc_info.value)


def test_resolve_unknown_profile_raises() -> None:
    cfg = _cfg({"only": Profile()})
    with pytest.raises(ProfileNotFound):
        resolve_profile(cfg, "ghost")


def test_resolve_unknown_parent_raises() -> None:
    cfg = _cfg({"child": Profile(extends="missing")})
    with pytest.raises(ProfileNotFound):
        resolve_profile(cfg, "child")
