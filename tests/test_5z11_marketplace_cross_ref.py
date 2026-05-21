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
from typer.testing import CliRunner

from setforge.cli import app
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
from setforge.errors import ConfigError, ValidationErrorWithContext
from setforge.local_overlay import LocalOverlayError, OverlayOrigin


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
# Round-3 boundary fix: empty/malformed plugin ref must NOT mask Check 6
# ---------------------------------------------------------------------------


def test_empty_plugin_pid_raises_local_overlay_error(tmp_path: Path) -> None:
    """Round-3 regression guard: ``_parse_overlay_plugin_pid`` must raise
    :class:`LocalOverlayError` (the resolver-phase sentinel), NOT bare
    :class:`ConfigError`, on an empty / whitespace plugin reference.

    The bare-``ConfigError`` raise path would have routed through the
    third arm of :func:`_apply_local_overlay_check` and signalled
    "cross-ref ran" — silently skipping Check 6 even though the
    mutation phase aborted before the cross-ref invariant could fire.
    Routing it as ``LocalOverlayError`` keeps the boundary correct:
    resolver-phase failure → Check 6 fallback executes.
    """
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
            - ""
        """,
    )
    with pytest.raises(LocalOverlayError) as exc_info:
        apply_local_overlay(cfg, rp, "p", local_config_path=local)
    assert "empty / whitespace plugin reference" in str(exc_info.value)


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
    assert all(p.origin is OverlayOrigin.PROFILE for p in resolution.plugins)
    assert all(m.origin is OverlayOrigin.PROFILE for m in resolution.marketplaces)


# ---------------------------------------------------------------------------
# Round-2 boundary fix: malformed local.yaml must NOT mask Check 6
# ---------------------------------------------------------------------------


def test_apply_local_overlay_check_returns_false_on_load_phase_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-2 regression guard: ``_apply_local_overlay_check`` MUST return
    ``False`` when the load phase fails so the caller falls back to Check 6.

    Round-1 fix-up flipped the ``overlay_cross_ref_ran`` gate on ALL
    ``ConfigError`` paths from :func:`apply_local_overlay`, including
    YAML-parse / Pydantic-shape failures that raise BEFORE
    :func:`_validate_overlay_marketplace_cross_ref` runs. That masked
    pre-existing marketplace inconsistencies on a malformed overlay.

    The sentinel :class:`LocalOverlayLoadError` plus the
    :func:`_apply_local_overlay_check` helper restores the boundary:
    a load-phase failure returns ``False`` (cross-ref did NOT run),
    so the caller MUST execute :func:`_check_marketplaces` as a
    fallback. This test asserts the boundary directly at the helper
    level — the CLI-end integration path through
    :func:`apply_preserve_user_keys_overlay` has a pre-existing
    documented gap (setforge-b1lg) that is out of scope here.
    """

    # cfg has a pre-existing marketplace inconsistency: ``sp`` references
    # ``ghost-mp``, but ``marketplaces:`` is empty. The standalone Check 6
    # would catch this — but only if the gate falls back correctly.
    cfg = _make_cfg(
        plugins={"sp": "ghost-mp"},
        marketplaces={},
    )
    rp = _make_resolved(["sp"])

    # Malformed YAML in the resolved local.yaml path forces
    # ``_load_overlay_blocks`` to raise ``LocalOverlayLoadError``.
    local = _write_local(tmp_path, "plugins: : :\n")

    failures: list[ValidationErrorWithContext | str] = []
    cross_ref_ran = _apply_local_overlay_check_with_path(
        cfg, rp, "p", "profile 'p'", failures, local, monkeypatch
    )

    # Boundary contract: load-phase failure must return ``False``.
    assert cross_ref_ran is False
    # The load-phase error itself was recorded under {ctx}.
    assert any("profile 'p'" in str(f) and "malformed YAML" in str(f) for f in failures)


@pytest.mark.parametrize(
    ("local_body", "expected_phrase"),
    [
        pytest.param(
            """\
            extensions:
              remove:
                - never-installed.foo
            """,
            "not in profile-resolved set",
            id="unknown-remove",
        ),
        pytest.param(
            """\
            plugins:
              add:
                - ""
            """,
            "empty / whitespace plugin reference",
            id="empty-plugin-pid",
        ),
    ],
)
def test_apply_local_overlay_check_returns_false_on_resolver_error(
    tmp_path: Path,
    local_body: str,
    expected_phrase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling boundary contract: a resolver-phase
    :class:`LocalOverlayError` ALSO returns ``False`` because the
    cross-ref check did NOT execute.

    Parametrized arms:
    - ``unknown-remove`` — well-formed YAML, but ``extensions.remove``
      points at an entry absent from the profile-resolved set.
    - ``empty-plugin-pid`` (round-3 regression guard) — well-formed
      YAML, but ``plugins.add`` contains an empty string; the parser
      ``_parse_overlay_plugin_pid`` MUST raise ``LocalOverlayError``
      (not bare ``ConfigError``) so this arm routes through the
      ``return False`` branch and Check 6 still runs.
    """

    cfg = _make_cfg(
        plugins={"sp": "ghost-mp"},
        marketplaces={},
    )
    rp = _make_resolved(["sp"], extensions=["ms-python.python"])

    local = _write_local(tmp_path, local_body)

    failures: list[ValidationErrorWithContext | str] = []
    cross_ref_ran = _apply_local_overlay_check_with_path(
        cfg, rp, "p", "profile 'p'", failures, local, monkeypatch
    )

    assert cross_ref_ran is False
    assert any(expected_phrase in str(f) for f in failures)


def test_apply_local_overlay_check_returns_true_on_cross_ref_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the cross-ref check itself raises (bare ``ConfigError``),
    the helper returns ``True`` so the caller skips Check 6 to avoid
    a duplicate failure row.
    """

    cfg = _make_cfg(
        plugins={"sp": "official"},
        marketplaces={
            "official": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="a/b"
            )
        },
    )
    rp = _make_resolved(["sp"])

    # Plugin add referencing an undefined marketplace — load succeeds,
    # resolvers succeed, mutation runs, cross-ref fires.
    local = _write_local(
        tmp_path,
        """\
        plugins:
          add:
            - new-plugin@nonexistent-mp
        """,
    )

    failures: list[ValidationErrorWithContext | str] = []
    cross_ref_ran = _apply_local_overlay_check_with_path(
        cfg, rp, "p", "profile 'p'", failures, local, monkeypatch
    )

    assert cross_ref_ran is True
    assert any("'nonexistent-mp'" in str(f) for f in failures)


def _apply_local_overlay_check_with_path(
    cfg: Config,
    resolved: ResolvedProfile,
    prof_name: str,
    ctx: str,
    failures: list[ValidationErrorWithContext | str],
    local_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> bool:
    """Run ``_apply_local_overlay_check`` against an explicit ``local.yaml`` path.

    ``_apply_local_overlay_check`` itself delegates to ``apply_local_overlay``,
    which reads :data:`setforge.source.LOCAL_CONFIG_PATH` by default. Patch
    the module-level constant via :class:`pytest.MonkeyPatch` so cleanup
    is automatic and the helper matches the companion CLI-integration
    test's monkeypatch convention.
    """
    from setforge.cli.validate import _apply_local_overlay_check

    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local_path)
    return _apply_local_overlay_check(cfg, resolved, prof_name, ctx, failures)


# ---------------------------------------------------------------------------
# Integration: resolver-phase failure surfaces both errors via the CLI
# ---------------------------------------------------------------------------


def test_resolver_error_via_cli_surfaces_check_6_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end CLI integration of the boundary fix.

    YAML parses cleanly (no b1lg integration gap) but the extensions
    resolver fires. The helper returns ``False`` → Check 6 runs →
    BOTH the resolver error AND the pre-existing marketplace
    inconsistency surface in the validate output.
    """
    cfg_yaml = dedent(
        """\
        version: 1
        tracked_files:
          d:
            src: tracked_file.txt
            dst: ~/.some-tracked_file
        marketplaces: {}
        claude_plugins:
          sp:
            marketplace: ghost-mp
        profiles:
          p:
            tracked_files: [d]
            claude_plugins: [sp]
        """
    )
    cfg_path = tmp_path / "setforge.yaml"
    cfg_path.write_text(cfg_yaml, encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    (tmp_path / "tracked" / "tracked_file.txt").write_text("x\n", encoding="utf-8")

    local = tmp_path / "local.yaml"
    local.write_text(
        dedent(
            """\
            extensions:
              remove:
                - never-installed.foo
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("setforge.cli.validate._LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local)

    result = CliRunner().invoke(
        app, ["validate", "--profile=p", f"--config={cfg_path}"]
    )
    assert result.exit_code == 1, result.output

    # Both errors surface: resolver failure + pre-existing marketplace
    # inconsistency from Check 6's fallback path.
    assert "not in profile-resolved set" in result.output
    assert "ghost-mp" in result.output
