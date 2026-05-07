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
    load_config,
)
from my_setup.errors import ConfigError

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
