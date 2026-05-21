"""Tests for the marketplace cross-ref check fired by apply_local_overlay.

Spec: ``setforge-5z11`` / SPEC 2. The cross-ref check fires at BOTH
``setforge validate`` (offline) AND ``setforge install`` (defensive
backstop) per Q8 — every plugin's resolved marketplace must exist in
``cfg.marketplaces`` joined with ``local.marketplaces.add``.

The error message names both the offending local.yaml entry AND the
resolved profile context's marketplace set, mirroring SPEC 2 mockup
shape (line 444-454 of the spec).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from setforge.config import (
    ClaudePluginRef,
    Config,
    Extensions,
    MarketplaceSource,
    MarketplaceSourceKind,
    Profile,
    ResolvedProfile,
    apply_local_overlay,
)
from setforge.errors import ConfigError
from setforge.local_overlay import LocalOverlayError


def _make_cfg(
    *,
    plugins: dict[str, str] | None = None,
    marketplaces: dict[str, MarketplaceSource] | None = None,
) -> Config:
    plugins = plugins or {}
    marketplaces = marketplaces or {}
    return Config(
        tracked_files={},
        marketplaces=marketplaces,
        claude_plugins={
            name: ClaudePluginRef(marketplace=mp) for name, mp in plugins.items()
        },
        profiles={"p": Profile(claude_plugins=list(plugins.keys()))},
    )


def _make_resolved(
    plugins: list[str], extensions: list[str] | None = None
) -> ResolvedProfile:
    return ResolvedProfile(
        claude_plugins=plugins,
        extensions=Extensions(include=extensions or []),
    )


def _write_local(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "local.yaml"
    p.write_text(dedent(body), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Cross-ref check: profile-side
# ---------------------------------------------------------------------------


def test_profile_only_plugin_with_existing_marketplace_passes(tmp_path: Path) -> None:
    """No local.yaml — pure profile plugin/marketplace passes cross-ref."""
    cfg = _make_cfg(
        plugins={"sp": "official"},
        marketplaces={
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="a/b"
            )
        },
    )
    rp = _make_resolved(["sp"])
    apply_local_overlay(cfg, rp, "p", local_config_path=tmp_path / "absent.yaml")


# ---------------------------------------------------------------------------
# Cross-ref check: overlay-add references undefined marketplace -> error
# ---------------------------------------------------------------------------


def test_overlay_plugin_add_references_undefined_marketplace_errors(
    tmp_path: Path,
) -> None:
    """SPEC 2 acceptance test: plugins.add[0] = 'p@bad-mp' must fail."""
    cfg = _make_cfg(
        plugins={"sp": "official"},
        marketplaces={
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="a/b"
            )
        },
    )
    rp = _make_resolved(["sp"])
    local = _write_local(
        tmp_path,
        """\
        plugins:
          add:
            - secure-code-review@nonexistent-marketplace
        """,
    )
    with pytest.raises(ConfigError) as exc_info:
        apply_local_overlay(cfg, rp, "p", local_config_path=local)
    msg = str(exc_info.value)
    assert "'secure-code-review'" in msg
    assert "'nonexistent-marketplace'" in msg
    assert "Available marketplaces" in msg


# ---------------------------------------------------------------------------
# Cross-ref check: overlay-added marketplace satisfies cross-ref
# ---------------------------------------------------------------------------


def test_overlay_marketplace_add_satisfies_cross_ref(tmp_path: Path) -> None:
    """When local.yaml adds the marketplace AND adds a plugin using it,
    the cross-ref check passes — the union is checked, not just profile."""
    cfg = _make_cfg(
        plugins={"sp": "official"},
        marketplaces={
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="a/b"
            )
        },
    )
    rp = _make_resolved(["sp"])
    local = _write_local(
        tmp_path,
        """\
        plugins:
          add:
            - work-tool@work-internal
        marketplaces:
          add:
            work-internal:
              source: github
              repo: work-corp/claude-plugins
        """,
    )
    # No exception expected.
    resolution = apply_local_overlay(cfg, rp, "p", local_config_path=local)
    # The work-internal marketplace landed in cfg.marketplaces:
    assert "work-internal" in cfg.marketplaces
    # work-tool@work-internal surfaces in resolution.plugins as LOCAL_ADD —
    # the resolver preserves the raw overlay.add string verbatim so the
    # renderer can print the name@marketplace form unchanged.
    from setforge.local_overlay import OverlayOrigin

    added = [p for p in resolution.plugins if p.origin is OverlayOrigin.LOCAL_ADD]
    assert any(p.value == "work-tool@work-internal" for p in added)
    # And the bare-name dispatch path still works: cfg.claude_plugins
    # carries the new entry too, so claude_plugins.reconcile resolves
    # work-tool -> work-internal transparently.
    assert "work-tool" in cfg.claude_plugins


# ---------------------------------------------------------------------------
# Cross-ref check: removing a marketplace that still has plugin refs errors
# ---------------------------------------------------------------------------


def test_marketplace_remove_leaving_orphaned_plugin_ref_errors(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(
        plugins={"sp": "official"},
        marketplaces={
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="a/b"
            ),
            "work-internal": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="work/x"
            ),
        },
    )
    rp = _make_resolved(["sp"])
    local = _write_local(
        tmp_path,
        """\
        marketplaces:
          remove:
            - official
        """,
    )
    with pytest.raises(ConfigError) as exc_info:
        apply_local_overlay(cfg, rp, "p", local_config_path=local)
    assert "'sp'" in str(exc_info.value)
    assert "'official'" in str(exc_info.value)


# ---------------------------------------------------------------------------
# add ∩ remove collision: plugins
# ---------------------------------------------------------------------------


def test_plugin_add_intersect_remove_collision_errors_via_apply(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg()
    rp = _make_resolved(["sp"])
    local = _write_local(
        tmp_path,
        """\
        plugins:
          add:
            - sp
          remove:
            - sp
        """,
    )
    with pytest.raises(LocalOverlayError) as exc_info:
        apply_local_overlay(cfg, rp, "p", local_config_path=local)
    assert "in both add and remove" in str(exc_info.value)


# ---------------------------------------------------------------------------
# remove-not-in-profile: extensions
# ---------------------------------------------------------------------------


def test_extension_remove_not_in_profile_errors_via_apply(tmp_path: Path) -> None:
    cfg = _make_cfg()
    rp = _make_resolved([], extensions=["ms-python.python"])
    local = _write_local(
        tmp_path,
        """\
        extensions:
          remove:
            - never-installed.foo
        """,
    )
    with pytest.raises(LocalOverlayError) as exc_info:
        apply_local_overlay(cfg, rp, "p", local_config_path=local)
    assert "not in profile-resolved set" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Mutation: overlay add/remove updates the resolved profile in place
# ---------------------------------------------------------------------------


def test_apply_local_overlay_mutates_resolved_extensions_in_place(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg()
    rp = _make_resolved([], extensions=["ms-python.python", "redhat.vscode-yaml"])
    local = _write_local(
        tmp_path,
        """\
        extensions:
          add:
            - vue.volar
          remove:
            - redhat.vscode-yaml
        """,
    )
    apply_local_overlay(cfg, rp, "p", local_config_path=local)
    assert "vue.volar" in rp.extensions.include
    assert "redhat.vscode-yaml" not in rp.extensions.include
    assert "ms-python.python" in rp.extensions.include


def test_apply_local_overlay_mutates_resolved_plugins_in_place(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(
        plugins={"sp": "official"},
        marketplaces={
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="a/b"
            )
        },
    )
    rp = _make_resolved(["sp"])
    local = _write_local(
        tmp_path,
        """\
        plugins:
          add:
            - other-tool@official
          remove:
            - sp
        """,
    )
    apply_local_overlay(cfg, rp, "p", local_config_path=local)
    assert "other-tool" in rp.claude_plugins
    assert "sp" not in rp.claude_plugins


def test_apply_local_overlay_synthesizes_claude_plugins_registry(
    tmp_path: Path,
) -> None:
    """plugins.add of name@mp adds an entry to cfg.claude_plugins so the
    existing bare-name dispatch in claude_plugins.reconcile is unchanged."""
    cfg = _make_cfg(
        plugins={"sp": "official"},
        marketplaces={
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="a/b"
            )
        },
    )
    rp = _make_resolved(["sp"])
    local = _write_local(
        tmp_path,
        """\
        plugins:
          add:
            - new-plugin@official
        """,
    )
    apply_local_overlay(cfg, rp, "p", local_config_path=local)
    assert "new-plugin" in cfg.claude_plugins
    assert cfg.claude_plugins["new-plugin"].marketplace == "official"


def test_apply_local_overlay_returns_empty_resolution_for_absent_local_yaml(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(
        plugins={"sp": "official"},
        marketplaces={
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="a/b"
            )
        },
    )
    rp = _make_resolved(["sp"])
    resolution = apply_local_overlay(
        cfg, rp, "p", local_config_path=tmp_path / "absent.yaml"
    )
    # Profile-only resolution — no LOCAL_ADD / LOCAL_REMOVE entries.
    from setforge.local_overlay import OverlayOrigin

    assert all(p.origin is OverlayOrigin.PROFILE for p in resolution.plugins)
    assert all(m.origin is OverlayOrigin.PROFILE for m in resolution.marketplaces)
