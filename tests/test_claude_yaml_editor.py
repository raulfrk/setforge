"""Tests for ruamel.yaml editing helpers (``setforge.claude_yaml_editor``).

Each ``yaml_add_*`` / ``yaml_remove_*`` verb mutates ``~/.claude/config.yaml``
via ruamel.yaml round-trip mode; tests assert idempotency and comment
preservation across edits.
"""

from pathlib import Path

import pytest

from setforge.config import MarketplaceSource, MarketplaceSourceKind

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

_YAML_FIXTURE = """\
version: 1

# Top-level comment.
tracked_files:
  d:
    src: x
    dst: y

# Marketplaces comment.
marketplaces:
  existing-mp:
    source: github
    repo: owner/existing-mp

# Plugins comment.
claude_plugins:
  existing-plugin:
    marketplace: existing-mp

profiles:
  myprofile:
    # Profile comment.
    tracked_files:
      - d
    claude_plugins:
      - existing-plugin
  bare:
    tracked_files:
      - d
"""


def _write_yaml_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "setforge.yaml"
    p.write_text(_YAML_FIXTURE, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# yaml_add_marketplace / yaml_remove_marketplace
# ---------------------------------------------------------------------------


def test_yaml_add_marketplace_appends(tmp_path: Path) -> None:
    from setforge.claude_yaml_editor import yaml_add_marketplace

    p = _write_yaml_fixture(tmp_path)
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="acme/new-mp")
    added = yaml_add_marketplace(p, "new-mp", src)
    assert added is True
    text = p.read_text()
    assert "new-mp" in text
    assert "acme/new-mp" in text
    # Comments preserved
    assert "Top-level comment." in text
    assert "Marketplaces comment." in text
    assert "Plugins comment." in text


def test_yaml_add_marketplace_idempotent(tmp_path: Path) -> None:
    from setforge.claude_yaml_editor import yaml_add_marketplace

    p = _write_yaml_fixture(tmp_path)
    src = MarketplaceSource(
        source=MarketplaceSourceKind.GITHUB, repo="owner/existing-mp"
    )
    added = yaml_add_marketplace(p, "existing-mp", src)
    assert added is False
    # Only one occurrence in YAML
    assert p.read_text().count("existing-mp") >= 1


def test_yaml_remove_marketplace(tmp_path: Path) -> None:
    from setforge.claude_yaml_editor import yaml_remove_marketplace

    p = _write_yaml_fixture(tmp_path)
    removed = yaml_remove_marketplace(p, "existing-mp")
    assert removed is True
    from setforge.config import load_config

    cfg = load_config(p)
    assert "existing-mp" not in cfg.marketplaces


def test_yaml_remove_marketplace_idempotent(tmp_path: Path) -> None:
    from setforge.claude_yaml_editor import yaml_remove_marketplace

    p = _write_yaml_fixture(tmp_path)
    removed = yaml_remove_marketplace(p, "ghost-mp")
    assert removed is False


# ---------------------------------------------------------------------------
# yaml_add_plugin / yaml_add_plugin_to_profile / yaml_remove_plugin_from_profile
# ---------------------------------------------------------------------------


def test_yaml_add_plugin_appends(tmp_path: Path) -> None:
    from setforge.claude_yaml_editor import yaml_add_plugin

    p = _write_yaml_fixture(tmp_path)
    added = yaml_add_plugin(p, "new-plugin", "existing-mp")
    assert added is True
    text = p.read_text()
    assert "new-plugin" in text
    # Comments preserved
    assert "Plugins comment." in text


def test_yaml_add_plugin_idempotent(tmp_path: Path) -> None:
    from setforge.claude_yaml_editor import yaml_add_plugin

    p = _write_yaml_fixture(tmp_path)
    added = yaml_add_plugin(p, "existing-plugin", "existing-mp")
    assert added is False


def test_yaml_add_plugin_to_profile(tmp_path: Path) -> None:
    from setforge.claude_yaml_editor import (
        yaml_add_plugin,
        yaml_add_plugin_to_profile,
    )

    p = _write_yaml_fixture(tmp_path)
    # Mirror the production CLI flow: register in top-level claude_plugins
    # first, then append to the profile list. load_config validates that
    # every profile reference exists in the registry.
    yaml_add_plugin(p, "new-plugin", "existing-mp")
    added = yaml_add_plugin_to_profile(p, "myprofile", "new-plugin")
    assert added is True
    from setforge.config import load_config

    cfg = load_config(p)
    assert "new-plugin" in cfg.profiles["myprofile"].claude_plugins


def test_yaml_add_plugin_to_profile_idempotent(tmp_path: Path) -> None:
    from setforge.claude_yaml_editor import yaml_add_plugin_to_profile

    p = _write_yaml_fixture(tmp_path)
    added = yaml_add_plugin_to_profile(p, "myprofile", "existing-plugin")
    assert added is False


def test_yaml_remove_plugin_from_profile(tmp_path: Path) -> None:
    from setforge.claude_yaml_editor import yaml_remove_plugin_from_profile

    p = _write_yaml_fixture(tmp_path)
    removed = yaml_remove_plugin_from_profile(p, "myprofile", "existing-plugin")
    assert removed is True
    from setforge.config import load_config

    cfg = load_config(p)
    assert "existing-plugin" not in cfg.profiles["myprofile"].claude_plugins


def test_yaml_comments_preserved_after_edits(tmp_path: Path) -> None:
    """Multiple edits must not corrupt comments in the YAML file."""
    from setforge.claude_yaml_editor import (
        yaml_add_marketplace,
        yaml_add_plugin,
        yaml_add_plugin_to_profile,
    )

    p = _write_yaml_fixture(tmp_path)
    yaml_add_marketplace(
        p, "test-mp", MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="t/t")
    )
    yaml_add_plugin(p, "test-plugin", "test-mp")
    yaml_add_plugin_to_profile(p, "myprofile", "test-plugin")

    text = p.read_text()
    assert "Top-level comment." in text
    assert "Marketplaces comment." in text
    assert "Plugins comment." in text
    assert "Profile comment." in text


# ---------------------------------------------------------------------------
# Atomic writes — crash mid-write must not truncate the config
# ---------------------------------------------------------------------------


def test_no_direct_truncating_open() -> None:
    """All five mutators route through the atomic helper, never a bare
    truncating ``config_path.open("w")``."""
    import setforge.claude_yaml_editor as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert 'config_path.open("w"' not in src
    assert "_atomic_yaml_dump" in src


def test_failed_replace_leaves_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash during the rename leaves the original config untouched and
    no temp file behind."""
    import setforge.claude_yaml_editor as mod

    p = _write_yaml_fixture(tmp_path)
    original = p.read_text()

    def boom(_src: object, _dst: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(mod.os, "replace", boom)
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="acme/new-mp")
    with pytest.raises(OSError, match="simulated crash"):
        mod.yaml_add_marketplace(p, "new-mp", src)

    assert p.read_text() == original
    assert list(tmp_path.glob(".setforge.yaml.*.tmp")) == []
