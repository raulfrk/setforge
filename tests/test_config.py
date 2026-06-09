"""Tests for config schema, YAML loading, and validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.config import (
    ClaudePluginRef,
    Config,
    Disposition,
    Extensions,
    MarketplaceSource,
    MarketplaceSourceKind,
    Profile,
    ReconcilePolicy,
    ResolvedProfile,
    TrackedFile,
    load_config,
    resolve_profile,
)
from setforge.errors import ConfigError, ProfileNotFound

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_sample_config() -> None:
    cfg = load_config(FIXTURES / "sample_config.yaml")
    assert cfg.version == 1
    assert set(cfg.tracked_files) == {"claude_md", "vscode_settings"}
    assert cfg.tracked_files["claude_md"].disposition is Disposition.SHARED
    assert cfg.tracked_files["vscode_settings"].disposition is Disposition.FORKED
    assert cfg.tracked_files["vscode_settings"].template is True
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
        # Intentional bad-string to assert pydantic rejects non-ReconcilePolicy values.
        Extensions(reconcile="yolo")  # type: ignore[arg-type]


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


def test_load_config_rejects_undeclared_plugin_reference(tmp_path: Path) -> None:
    """A profile referencing a plugin missing from the top-level
    claude_plugins registry raises ConfigError naming both the profile
    and the offending plugin, before any subprocess work runs."""
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(
        """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
marketplaces:
  official:
    source: github
    repo: a/b
claude_plugins:
  declared-plugin:
    marketplace: official
profiles:
  base:
    tracked_files:
      - d
    claude_plugins:
      - declared-plugin
      - missing-plugin
"""
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)
    msg = str(exc_info.value)
    assert "missing-plugin" in msg
    assert "base" in msg


def test_load_config_collects_multiple_undeclared_plugin_references(
    tmp_path: Path,
) -> None:
    """When several profiles reference undeclared plugins, all offenders
    appear in a single ConfigError message — no early-bail on the first."""
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(
        """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
profiles:
  alpha:
    tracked_files: [d]
    claude_plugins:
      - ghost-a
  beta:
    tracked_files: [d]
    claude_plugins:
      - ghost-b
"""
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)
    msg = str(exc_info.value)
    assert "ghost-a" in msg
    assert "ghost-b" in msg


def test_tracked_file_defaults() -> None:
    df = TrackedFile(src=Path("a"), dst="b")
    assert df.template is False
    assert df.disposition is None
    assert df.spans == []


def test_tracked_file_rejects_tab_in_src() -> None:
    """Tab in src would corrupt the unified-diff format used by
    transitions; reject at config-load time with the offending byte
    surfaced as ``\\xNN`` for diagnosability."""
    with pytest.raises(ValidationError) as exc_info:
        TrackedFile(src=Path("path/with\ttab"), dst="~/x")
    assert "\\x09" in str(exc_info.value)


def test_tracked_file_rejects_newline_in_dst() -> None:
    """Same hazard via ``dst``; ensure both fields are guarded."""
    with pytest.raises(ValidationError) as exc_info:
        TrackedFile(src=Path("ok"), dst="bad\npath")
    assert "\\x0a" in str(exc_info.value)


def test_tracked_file_accepts_paths_with_spaces_and_unicode() -> None:
    """Negative test guarding against over-rejection: spaces and
    non-ASCII (C1+) characters are valid in real paths."""
    df = TrackedFile(src=Path("my path/with spaces.txt"), dst="~/some/é-named/file")
    assert df.dst == "~/some/é-named/file"


def test_profile_defaults() -> None:
    p = Profile()
    assert p.extends is None
    assert p.tracked_files == []
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
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        profiles=profiles,
    )


def test_resolve_single_profile() -> None:
    cfg = _cfg({"only": Profile(tracked_files=["x", "y"])})
    resolved = resolve_profile(cfg, "only")
    assert isinstance(resolved, ResolvedProfile)
    assert resolved.tracked_files == ["x", "y"]
    assert resolved.extends is None


def test_resolve_two_level_chain_lists_and_scalars() -> None:
    cfg = _cfg(
        {
            "parent": Profile(
                tracked_files=["a", "b"],
                claude_plugins=["p1"],
                extensions=Extensions(include=["e1"], reconcile=ReconcilePolicy.PRUNE),
                plugins_reconcile=ReconcilePolicy.PRUNE,
            ),
            "child": Profile(
                extends="parent",
                tracked_files=["b", "c"],
                claude_plugins=["p2"],
                extensions=Extensions(include=["e2"]),
            ),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.tracked_files == ["a", "b", "c"]
    assert resolved.claude_plugins == ["p1", "p2"]
    assert resolved.extensions.include == ["e1", "e2"]
    assert resolved.extensions.reconcile is ReconcilePolicy.PRUNE
    assert resolved.plugins_reconcile is ReconcilePolicy.PRUNE


def test_resolve_three_level_chain() -> None:
    cfg = _cfg(
        {
            "grand": Profile(tracked_files=["g"]),
            "parent": Profile(extends="grand", tracked_files=["p"]),
            "child": Profile(extends="parent", tracked_files=["c"]),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.tracked_files == ["g", "p", "c"]


def test_resolve_dedup_preserves_first_occurrence() -> None:
    cfg = _cfg(
        {
            "parent": Profile(tracked_files=["a", "b"]),
            "child": Profile(extends="parent", tracked_files=["a", "c", "b"]),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.tracked_files == ["a", "b", "c"]


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


def test_tracked_file_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TrackedFile.model_validate({"src": "a", "dst": "b", "typo": True})


def test_profile_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Profile.model_validate({"extens": "base"})


def test_extensions_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Extensions.model_validate({"includ": ["x"]})


def test_marketplace_source_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MarketplaceSource.model_validate(
            {"source": "github", "repo": "a/b", "extra": 1}
        )


def test_claude_plugin_ref_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClaudePluginRef.model_validate({"marketplace": "m", "version": "1.0"})


def test_resolved_profile_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResolvedProfile.model_validate({"unknown": True})


def test_config_rejects_unknown_top_level_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Config.model_validate(
            {
                "tracked_files": {"a": {"src": "x", "dst": "y"}},
                "profiles": {"p": {}},
                "stray_top_level": 1,
            }
        )


def test_config_rejects_unknown_field_in_nested_tracked_file() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Config.model_validate(
            {
                "tracked_files": {"a": {"src": "x", "dst": "y", "tipo": True}},
                "profiles": {"p": {}},
            }
        )


# ---------------------------------------------------------------------------
# local.yaml tracked_files overlay schema
# ---------------------------------------------------------------------------


def test_local_tracked_files_overlay_defaults_to_empty_dict() -> None:
    """Absent ``tracked_files:`` -> empty dict; existing source-only loads
    must continue to work unchanged."""
    from setforge.source import _LocalSourceConfig

    cfg = _LocalSourceConfig.model_validate({})
    assert cfg.tracked_files == {}


def test_local_tracked_files_overlay_rejects_unknown_field() -> None:
    """``model_config = extra='forbid'`` posture extends to overlay models —
    typos in local.yaml surface at validate time rather than silently."""
    from setforge.source import _LocalSourceConfig

    with pytest.raises(ValidationError):
        _LocalSourceConfig.model_validate(
            {
                "tracked_files": {
                    "vscode_serv_settings": {
                        "disposition": "shared",
                        "unknown_field": ["b"],
                    }
                }
            }
        )


def test_local_tracked_files_overlay_accepts_disposition() -> None:
    """A host-local ``disposition`` override validates cleanly via
    :class:`_LocalSourceConfig`."""
    from setforge.source import _LocalSourceConfig

    cfg = _LocalSourceConfig.model_validate(
        {"tracked_files": {"vscode": {"disposition": "forked"}}}
    )
    overlay = cfg.tracked_files["vscode"]
    assert overlay.disposition is Disposition.FORKED
