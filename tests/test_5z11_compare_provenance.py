"""Tests for SPEC 2 compare output provenance rendering.

Validates :func:`setforge.compare.render_local_overlay_block` — the
per-axis (Claude plugins / VSCode extensions / Marketplaces) effective
set with [from local.yaml] / U+2212-prefixed remove tags, plus the
footer summary line ``[Host overlay summary: ...]`` per Q9 Shape A.

The renderer routes ALL tag wording through
:func:`setforge.local_overlay.display_tag` — these tests fix the
expected output strings so a future regression that bypasses
``display_tag`` surfaces here.
"""

from __future__ import annotations

from setforge.compare import render_local_overlay_block
from setforge.config import (
    Config,
    LocalOverlayResolution,
    MarketplaceSource,
    MarketplaceSourceKind,
    Profile,
)
from setforge.local_overlay import (
    OverlayOrigin,
    ResolvedExtension,
    ResolvedMarketplace,
    ResolvedPlugin,
)


def _cfg_with_marketplaces(
    mps: dict[str, MarketplaceSource] | None = None,
) -> Config:
    return Config(
        tracked_files={},
        marketplaces=mps or {},
        claude_plugins={},
        profiles={"p": Profile()},
    )


def test_render_empty_resolution_returns_empty_list() -> None:
    resolution = LocalOverlayResolution(
        plugins=[], extensions=[], marketplaces=[], empty=True
    )
    cfg = _cfg_with_marketplaces()
    assert render_local_overlay_block(cfg, resolution) == []


def test_render_pure_profile_resolution_returns_empty_list() -> None:
    """No overlay = no rendering — even with profile-side entries."""
    resolution = LocalOverlayResolution(
        plugins=[ResolvedPlugin("sp", OverlayOrigin.PROFILE)],
        extensions=[ResolvedExtension("ms-python.python", OverlayOrigin.PROFILE)],
        marketplaces=[ResolvedMarketplace("official", OverlayOrigin.PROFILE)],
        empty=False,
    )
    cfg = _cfg_with_marketplaces(
        {"official": MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="a/b")}
    )
    assert render_local_overlay_block(cfg, resolution) == []


def test_render_local_add_plugin_carries_from_local_yaml_tag() -> None:
    resolution = LocalOverlayResolution(
        plugins=[
            ResolvedPlugin("secure-code-review@work-internal", OverlayOrigin.LOCAL_ADD),
        ],
        extensions=[],
        marketplaces=[],
        empty=False,
    )
    lines = render_local_overlay_block(_cfg_with_marketplaces(), resolution)
    rendered = "\n".join(lines)
    assert "Claude plugins:" in rendered
    assert "+ secure-code-review@work-internal [from local.yaml]" in rendered


def test_render_local_remove_plugin_carries_remove_tag_with_unicode_minus() -> None:
    resolution = LocalOverlayResolution(
        plugins=[
            ResolvedPlugin("unwanted-default-plugin", OverlayOrigin.LOCAL_REMOVE),
        ],
        extensions=[],
        marketplaces=[],
        empty=False,
    )
    lines = render_local_overlay_block(_cfg_with_marketplaces(), resolution)
    rendered = "\n".join(lines)
    minus = chr(0x2212)
    expected_tag = f"[{minus} removed via local.yaml]"
    assert expected_tag in rendered
    # The row marker is ALSO U+2212 (not ASCII '-') for column-width parity.
    assert f"{minus} unwanted-default-plugin {expected_tag}" in rendered


def test_render_extension_local_add_carries_tag() -> None:
    resolution = LocalOverlayResolution(
        plugins=[],
        extensions=[
            ResolvedExtension("ms-python.python", OverlayOrigin.LOCAL_ADD),
        ],
        marketplaces=[],
        empty=False,
    )
    lines = render_local_overlay_block(_cfg_with_marketplaces(), resolution)
    rendered = "\n".join(lines)
    assert "VSCode extensions:" in rendered
    assert "+ ms-python.python [from local.yaml]" in rendered


def test_render_marketplace_local_add_carries_source_details() -> None:
    resolution = LocalOverlayResolution(
        plugins=[],
        extensions=[],
        marketplaces=[
            ResolvedMarketplace("work-internal", OverlayOrigin.LOCAL_ADD),
        ],
        empty=False,
    )
    cfg = _cfg_with_marketplaces(
        {
            "work-internal": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="work-corp/claude-plugins"
            )
        }
    )
    lines = render_local_overlay_block(cfg, resolution)
    rendered = "\n".join(lines)
    assert "Marketplaces:" in rendered
    assert (
        "+ work-internal {source: github, repo: work-corp/claude-plugins} "
        "[from local.yaml]" in rendered
    )


def test_render_footer_summary_carries_per_axis_counts() -> None:
    """Q9 Shape A: footer summary line carries +adds/-removes per axis."""
    resolution = LocalOverlayResolution(
        plugins=[
            ResolvedPlugin("x", OverlayOrigin.LOCAL_ADD),
            ResolvedPlugin("y", OverlayOrigin.LOCAL_REMOVE),
        ],
        extensions=[
            ResolvedExtension("e1", OverlayOrigin.LOCAL_ADD),
            ResolvedExtension("e2", OverlayOrigin.LOCAL_REMOVE),
        ],
        marketplaces=[
            ResolvedMarketplace("m1", OverlayOrigin.LOCAL_ADD),
        ],
        empty=False,
    )
    cfg = _cfg_with_marketplaces(
        {"m1": MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="a/b")}
    )
    lines = render_local_overlay_block(cfg, resolution)
    rendered = "\n".join(lines)
    minus = chr(0x2212)
    expected_summary = (
        f"[Host overlay summary: plugins 1+/1{minus}; "
        f"extensions 1+/1{minus}; marketplaces 1+/0{minus} via local.yaml]"
    )
    assert expected_summary in rendered


def test_render_full_mockup_shape() -> None:
    """Snapshot test: combined mockup from SPEC 2 section 416-435."""
    resolution = LocalOverlayResolution(
        plugins=[
            ResolvedPlugin("secure-code-review@work-internal", OverlayOrigin.LOCAL_ADD),
            ResolvedPlugin("general-code-helper", OverlayOrigin.PROFILE),
            ResolvedPlugin("unwanted-default-plugin", OverlayOrigin.LOCAL_REMOVE),
        ],
        extensions=[
            ResolvedExtension("ms-python.python", OverlayOrigin.LOCAL_ADD),
            ResolvedExtension("claude-code.claude-code", OverlayOrigin.PROFILE),
            ResolvedExtension("redhat.vscode-yaml", OverlayOrigin.LOCAL_REMOVE),
        ],
        marketplaces=[
            ResolvedMarketplace("official", OverlayOrigin.PROFILE),
            ResolvedMarketplace("work-internal", OverlayOrigin.LOCAL_ADD),
        ],
        empty=False,
    )
    cfg = _cfg_with_marketplaces(
        {
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB,
                repo="anthropics/claude-plugins",
            ),
            "work-internal": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB,
                repo="work-corp/claude-plugins",
            ),
        }
    )
    lines = render_local_overlay_block(cfg, resolution)
    rendered = "\n".join(lines)
    minus = chr(0x2212)
    # Header lines for each axis
    assert "Claude plugins:" in rendered
    assert "VSCode extensions:" in rendered
    assert "Marketplaces:" in rendered
    # Mockup verbatim lines
    assert "+ secure-code-review@work-internal [from local.yaml]" in rendered
    assert "+ general-code-helper" in rendered
    assert (
        f"{minus} unwanted-default-plugin [{minus} removed via local.yaml]" in rendered
    )
    assert "+ ms-python.python [from local.yaml]" in rendered
    assert "+ claude-code.claude-code" in rendered
    yaml_remove_line = f"{minus} redhat.vscode-yaml [{minus} removed via local.yaml]"
    assert yaml_remove_line in rendered
    assert (
        "+ work-internal {source: github, repo: work-corp/claude-plugins} "
        "[from local.yaml]" in rendered
    )
    assert (
        f"[Host overlay summary: plugins 1+/1{minus}; "
        f"extensions 1+/1{minus}; marketplaces 1+/0{minus} via local.yaml]"
    ) in rendered


def test_render_uses_display_tag_for_wording() -> None:
    """The renderer's output MUST match display_tag() for every origin —
    a future inline f-string that drifts from display_tag's literal would
    fail this comparison.
    """
    from setforge.local_overlay import display_tag

    resolution = LocalOverlayResolution(
        plugins=[
            ResolvedPlugin("x", OverlayOrigin.LOCAL_ADD),
            ResolvedPlugin("y", OverlayOrigin.LOCAL_REMOVE),
        ],
        extensions=[],
        marketplaces=[],
        empty=False,
    )
    rendered = "\n".join(
        render_local_overlay_block(_cfg_with_marketplaces(), resolution)
    )
    # Every produced tag-substring must be one of the display_tag returns.
    assert display_tag(OverlayOrigin.LOCAL_ADD) in rendered
    assert display_tag(OverlayOrigin.LOCAL_REMOVE) in rendered
