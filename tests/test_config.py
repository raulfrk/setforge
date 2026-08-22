"""Tests for config schema, YAML loading, and validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge import reconcile_adapter
from setforge.config import (
    ClaudePluginRef,
    CodexProfile,
    CodexSpec,
    Config,
    ExtensionPackage,
    ExtensionReconcile,
    Extensions,
    MarketplaceSource,
    MarketplaceSourceKind,
    PluginPackage,
    PluginReconcile,
    Profile,
    ReconcilePolicy,
    ReconcileSpec,
    ResolvedProfile,
    TrackedFile,
    apply_host_local_codex_overlay,
    guard_minimum_version,
    load_config,
    resolve_effective_profile,
    resolve_profile,
)
from setforge.errors import ConfigError, ProfileNotFound

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_sample_config() -> None:
    cfg = load_config(FIXTURES / "sample_config.yaml")
    assert cfg.version == 1
    assert set(cfg.tracked_files) == {"claude_md", "vscode_settings"}
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
    cfg = _cfg(
        {
            "base": Profile(
                tracked_files=["claude_md"],
                reconcile=ReconcileSpec(
                    extensions=ExtensionReconcile(policy="additive"),  # type: ignore[arg-type]
                ),
            ),
            "child": Profile(
                extends="base",
                reconcile=ReconcileSpec(
                    extensions=ExtensionReconcile(policy="prune"),  # type: ignore[arg-type]
                ),
            ),
        }
    )
    assert cfg.profiles["base"].reconcile.extensions.policy is ReconcilePolicy.ADDITIVE
    assert cfg.profiles["child"].reconcile.extensions.policy is ReconcilePolicy.PRUNE


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


def test_load_config_malformed_yaml(tmp_path: Path) -> None:
    # A YAML syntax error must surface as a clean ConfigError (naming the
    # file), not a raw ruamel ParserError/ScannerError traceback — the
    # docstring promises ConfigError on parse errors.
    bad = tmp_path / "bad.yaml"
    bad.write_text("profiles: [unterminated\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)


def test_load_config_non_utf8(tmp_path: Path) -> None:
    # A non-UTF-8 config must also refuse cleanly (ConfigError), parity with
    # the YAML-parse path and detect_current_schema — not a raw
    # UnicodeDecodeError traceback.
    bad = tmp_path / "latin1.yaml"
    bad.write_bytes(b"greeting: caf\xe9\n")  # 0xe9 is invalid UTF-8
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)


def test_guard_minimum_version_malformed_yaml(tmp_path: Path) -> None:
    # guard_minimum_version runs on `setforge migrate` BEFORE the hardened
    # detect_current_schema, so its yaml.load must also clean-refuse a
    # malformed config with ConfigError, not a raw ruamel traceback.
    bad = tmp_path / "bad.yaml"
    bad.write_text("minimum_version: [unterminated\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        guard_minimum_version(bad)


def test_load_config_rejects_undeclared_plugin_reference(tmp_path: Path) -> None:
    """A profile referencing (via a plugin package) a plugin missing from
    the top-level claude_plugins registry raises ConfigError naming both
    the profile and the offending plugin, before any subprocess work runs."""
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
packages:
  declared-pkg:
    type: plugin
    plugin: declared-plugin
  missing-pkg:
    type: plugin
    plugin: missing-plugin
profiles:
  base:
    tracked_files:
      - d
    packages:
      - declared-pkg
      - missing-pkg
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
    """When several profiles reference undeclared plugins (via plugin
    packages), all offenders appear in a single ConfigError message — no
    early-bail on the first."""
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(
        """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
packages:
  ghost-a-pkg:
    type: plugin
    plugin: ghost-a
  ghost-b-pkg:
    type: plugin
    plugin: ghost-b
profiles:
  alpha:
    tracked_files: [d]
    packages:
      - ghost-a-pkg
  beta:
    tracked_files: [d]
    packages:
      - ghost-b-pkg
"""
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)
    msg = str(exc_info.value)
    assert "ghost-a" in msg
    assert "ghost-b" in msg


def test_load_config_rejects_undeclared_plugin_via_package(tmp_path: Path) -> None:
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
packages:
  ghostpkg:
    type: plugin
    plugin: missing-via-package
profiles:
  base:
    tracked_files: [d]
    packages: [ghostpkg]
"""
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)
    msg = str(exc_info.value)
    assert "undeclared plugin(s)" in msg
    assert "missing-via-package" in msg
    assert "base" in msg


def test_tracked_file_defaults() -> None:
    df = TrackedFile(src=Path("a"), dst="b")
    assert df.template is False


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
    assert p.packages == []
    assert p.reconcile == ReconcileSpec()
    assert p.reconcile.plugins.policy is ReconcilePolicy.ADDITIVE
    assert p.reconcile.extensions.policy is ReconcilePolicy.ADDITIVE
    assert p.reconcile.extensions.exclude == []
    assert p.bootstrap == []


def test_claude_plugin_ref() -> None:
    ref = ClaudePluginRef(marketplace="official")
    assert ref.marketplace == "official"


def test_config_round_trip_via_model() -> None:
    cfg = load_config(FIXTURES / "sample_config.yaml")
    dumped = cfg.model_dump()
    reloaded = Config.model_validate(dumped)
    assert reloaded == cfg


def test_claude_only_dump_omits_codex_namespaces() -> None:
    cfg = load_config(FIXTURES / "sample_config.yaml")

    assert "codex" not in cfg.model_dump()
    assert all(
        "codex" not in profile for profile in cfg.model_dump()["profiles"].values()
    )
    assert "codex" not in resolve_profile(cfg, "child").model_dump()


def test_codex_profile_inheritance_is_parent_first_and_deduplicated() -> None:
    cfg = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "skills": {
                    "review": {"source": "codex/review", "scope": "user"},
                    "test": {"source": "codex/test", "scope": "user"},
                }
            }
        ),
        profiles={
            "parent": Profile(codex=CodexProfile(skills=["review", "test"])),
            "child": Profile(
                extends="parent", codex=CodexProfile(skills=["test", "review"])
            ),
        },
    )

    assert resolve_profile(cfg, "child").codex == CodexProfile(
        skills=["review", "test"]
    )


def test_codex_reconcile_policy_inherits_and_explicitly_overrides() -> None:
    parent = CodexProfile(reconcile=PluginReconcile(policy=ReconcilePolicy.PRUNE))
    inherited = Config(
        tracked_files={},
        profiles={
            "parent": Profile(codex=parent),
            "child": Profile(extends="parent", codex=CodexProfile()),
        },
    )
    overridden = Config(
        tracked_files={},
        profiles={
            "parent": Profile(codex=parent),
            "child": Profile(
                extends="parent",
                codex=CodexProfile(
                    reconcile=PluginReconcile(policy=ReconcilePolicy.ADDITIVE)
                ),
            ),
        },
    )

    assert resolve_profile(inherited, "child").codex == parent
    assert resolve_profile(overridden, "child").codex == CodexProfile(
        reconcile=PluginReconcile(policy=ReconcilePolicy.ADDITIVE)
    )


def test_local_codex_overlay_merges_add_then_remove(tmp_path: Path) -> None:
    cfg = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "skills": {
                    "base": {"source": "codex/base"},
                    "local": {"source": "codex/local"},
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile(skills=["base"]))},
    )
    path = tmp_path / "local.yaml"
    path.write_text("codex:\n  skills:\n    add: [local, base]\n    remove: [base]\n")

    resolved = apply_host_local_codex_overlay(
        cfg, resolve_profile(cfg, "default"), local_config_path=path
    )

    assert resolved.codex == CodexProfile(skills=["local"])


def test_local_codex_overlay_resolves_portable_project_locator(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cfg = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "config": {
                    "project": {
                        "source": "codex/project.toml",
                        "scope": "project",
                        "project": "app",
                    }
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile(config=["project"]))},
    )
    path = tmp_path / "local.yaml"
    path.write_text(f"codex:\n  project_paths:\n    app: {project}\n")

    apply_host_local_codex_overlay(
        cfg, resolve_profile(cfg, "default"), local_config_path=path
    )

    assert cfg.codex is not None
    assert cfg.codex.config["project"].project == Path("app")
    assert cfg._codex_project_paths == {"app": project}

    apply_host_local_codex_overlay(
        cfg, resolve_profile(cfg, "default"), local_config_path=path
    )
    assert cfg.codex.config["project"].project == Path("app")


def test_local_codex_overlay_requires_selected_project_mapping(tmp_path: Path) -> None:
    cfg = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "config": {
                    "project": {
                        "source": "codex/project.toml",
                        "scope": "project",
                        "project": "app",
                    }
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile(config=["project"]))},
    )
    path = tmp_path / "local.yaml"
    path.write_text("codex: {}\n")

    with pytest.raises(ConfigError, match=r"project_paths.*app"):
        apply_host_local_codex_overlay(
            cfg, resolve_profile(cfg, "default"), local_config_path=path
        )


@pytest.mark.parametrize("body", [None, "", "codex: {}\n"])
def test_local_codex_overlay_noop_preserves_absent_namespace(
    tmp_path: Path, body: str | None
) -> None:
    cfg = Config(tracked_files={}, profiles={"default": Profile()})
    path = tmp_path / "local.yaml"
    if body is not None:
        path.write_text(body)

    resolved = apply_host_local_codex_overlay(
        cfg, resolve_profile(cfg, "default"), local_config_path=path
    )

    assert resolved.codex is None
    assert "codex" not in resolved.model_dump()


def test_effective_profile_applies_local_codex_selections(tmp_path: Path) -> None:
    (tmp_path / "tracked/codex/review").mkdir(parents=True)
    (tmp_path / "tracked/codex/review/SKILL.md").write_text("# Review\n")
    cfg = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {"skills": {"review": {"source": "codex/review"}}}
        ),
        profiles={"default": Profile()},
    )
    path = tmp_path / "local.yaml"
    path.write_text("codex:\n  skills:\n    add: [review]\n")

    effective = resolve_effective_profile(
        cfg, "default", tmp_path, local_config_path=path
    )

    assert effective.resolved.codex == CodexProfile(skills=["review"])


def test_effective_profile_rejects_unknown_local_codex_selection(
    tmp_path: Path,
) -> None:
    cfg = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec(),
        profiles={"default": Profile()},
    )
    path = tmp_path / "local.yaml"
    path.write_text("codex:\n  skills:\n    add: [missing]\n")

    with pytest.raises(ConfigError, match=r"local overlay\.codex\.skills\[missing\]"):
        resolve_effective_profile(cfg, "default", tmp_path, local_config_path=path)


def test_effective_profile_rejects_local_codex_ownership_collision(
    tmp_path: Path,
) -> None:
    cfg = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "instructions": {
                    "base": {"source": "codex/base.md"},
                    "local": {"source": "codex/local.md"},
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile(instructions=["base"]))},
    )
    path = tmp_path / "local.yaml"
    path.write_text("codex:\n  instructions:\n    add: [local]\n")

    with pytest.raises(ConfigError, match="conflicting Codex ownership"):
        resolve_effective_profile(cfg, "default", tmp_path, local_config_path=path)


def test_load_config_allows_same_bare_name_across_products(tmp_path: Path) -> None:
    path = tmp_path / "setforge.yaml"
    path.write_text(
        """version: 1
schema_version: '6.4'
minimum_version: '6.4'
marketplaces:
  official: {source: github, repo: anthropics/plugins}
claude_plugins:
  review: {marketplace: official}
codex:
  marketplaces:
    official: {source: github, repo: openai/plugins}
  plugins:
    review: {marketplace: official}
profiles:
  default:
    codex:
      plugins: [review]
tracked_files: {}
"""
    )

    cfg = load_config(path)

    assert cfg.claude_plugins["review"].marketplace == "official"
    assert cfg.codex is not None
    assert cfg.codex.plugins["review"].marketplace == "official"


@pytest.mark.parametrize(
    ("schema_version", "minimum_version"),
    [("6.3", "6.4"), ("6.4", None), ("6.4", "6.3")],
)
def test_codex_contract_requires_schema_and_floor_6_4(
    tmp_path: Path, schema_version: str, minimum_version: str | None
) -> None:
    path = tmp_path / "setforge.yaml"
    floor = "" if minimum_version is None else f"minimum_version: '{minimum_version}'\n"
    path.write_text(
        f"version: 1\nschema_version: '{schema_version}'\n{floor}"
        "codex: {}\ntracked_files: {}\nprofiles: {}\n"
    )

    with pytest.raises(ConfigError, match="Codex resources require"):
        load_config(path)


def test_codex_references_are_product_qualified(tmp_path: Path) -> None:
    path = tmp_path / "setforge.yaml"
    path.write_text(
        """version: 1
schema_version: '6.4'
minimum_version: '6.4'
codex:
  plugins:
    review: {marketplace: missing}
profiles:
  default:
    codex:
      skills: [missing]
tracked_files: {}
"""
    )

    with pytest.raises(
        ConfigError, match=r"default\.codex\.skills\[missing\].*codex\.plugins\.review"
    ):
        load_config(path)


def test_codex_selected_resources_must_have_unique_native_ownership(
    tmp_path: Path,
) -> None:
    path = tmp_path / "setforge.yaml"
    path.write_text(
        """version: 1
schema_version: '6.4'
minimum_version: '6.4'
tracked_files: {}
codex:
  instructions:
    first: {source: codex/first.md}
    second: {source: codex/second.md}
profiles:
  default:
    codex:
      instructions: [first, second]
"""
    )

    with pytest.raises(ConfigError, match="conflicting Codex ownership"):
        load_config(path)


@pytest.mark.parametrize(
    "payload",
    [
        {"config": {"bad": {"source": "x", "scope": "project"}}},
        {"config": {"bad": {"source": "C:/Users/alice/config.toml"}}},
        {"config": {"bad": {"source": "\\\\server\\share\\config.toml"}}},
        {"instructions": {"bad": {"source": "x", "scope": "user", "project": "repo"}}},
        {
            "skills": {
                "bad": {"source": "x", "scope": "repository", "project": "../repo"}
            }
        },
        {
            "mcp_servers": {
                "bad": {
                    "transport": "stdio",
                    "command": "x",
                    "env": {"TOKEN": "secret"},
                }
            }
        },
        {
            "mcp_servers": {
                "bad": {
                    "transport": "http",
                    "url": "https://example.com:bad/mcp",
                }
            }
        },
        {
            "mcp_servers": {
                "bad": {
                    "transport": "http",
                    "url": "https://example.com\\evil/mcp",
                }
            }
        },
        {
            "mcp_servers": {
                "bad": {
                    "transport": "http",
                    "url": "https://example.com/a b",
                }
            }
        },
        {
            "mcp_servers": {
                "bad": {
                    "transport": "stdio",
                    "command": "x",
                    "env_vars": ["TOKEN.DOT"],
                }
            }
        },
        {
            "mcp_servers": {
                "bad": {
                    "transport": "http",
                    "url": "https://user:password@example.com/mcp",
                }
            }
        },
        {
            "mcp_servers": {
                "bad": {
                    "transport": "http",
                    "url": "https://example.com",
                    "http_headers": {"Authorization": "secret"},
                }
            }
        },
    ],
)
def test_codex_contract_rejects_ambiguous_paths_and_literal_secrets(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CodexSpec.model_validate(payload)


def test_codex_mcp_transports_and_approval_policy_round_trip_without_secrets() -> None:
    spec = CodexSpec.model_validate(
        {
            "mcp_servers": {
                "local": {
                    "transport": "stdio",
                    "command": "uvx",
                    "args": ["server"],
                    "cwd": "tools/server",
                    "env_vars": ["LOCAL_TOKEN"],
                    "default_tools_approval_mode": "prompt",
                    "tools": {"read": {"approval_mode": "auto"}},
                },
                "remote": {
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "bearer_token_env_var": "REMOTE_TOKEN",
                    "env_http_headers": {"X-Org": "ORG_ID"},
                    "default_tools_approval_mode": "writes",
                },
            }
        }
    )

    dumped = spec.model_dump(mode="json")

    assert CodexSpec.model_validate(dumped) == spec
    assert "secret" not in repr(dumped).lower()
    assert dumped["mcp_servers"]["remote"]["bearer_token_env_var"] == "REMOTE_TOKEN"


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
    cfg = Config(
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        claude_plugins={
            "p1": ClaudePluginRef(marketplace="m"),
            "p2": ClaudePluginRef(marketplace="m"),
        },
        packages={
            "p1-pkg": PluginPackage(plugin="p1"),
            "p2-pkg": PluginPackage(plugin="p2"),
            "e1-pkg": ExtensionPackage(extension="e1"),
            "e2-pkg": ExtensionPackage(extension="e2"),
        },
        profiles={
            "parent": Profile(
                tracked_files=["a", "b"],
                packages=["p1-pkg", "e1-pkg"],
                reconcile=ReconcileSpec(
                    plugins=PluginReconcile(policy=ReconcilePolicy.PRUNE),
                    extensions=ExtensionReconcile(policy=ReconcilePolicy.PRUNE),
                ),
            ),
            "child": Profile(
                extends="parent",
                tracked_files=["b", "c"],
                packages=["p2-pkg", "e2-pkg"],
            ),
        },
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.tracked_files == ["a", "b", "c"]
    assert reconcile_adapter.plugin_bare_names(cfg, resolved) == ["p1", "p2"]
    assert reconcile_adapter.extensions_input(cfg, resolved).include == ["e1", "e2"]
    assert resolved.reconcile.extensions.policy is ReconcilePolicy.PRUNE
    assert resolved.reconcile.plugins.policy is ReconcilePolicy.PRUNE


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
            "parent": Profile(
                reconcile=ReconcileSpec(
                    plugins=PluginReconcile(policy=ReconcilePolicy.PRUNE),
                ),
            ),
            "child": Profile(extends="parent"),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.plugins.policy is ReconcilePolicy.PRUNE


def test_resolve_scalar_child_explicit_override() -> None:
    cfg = _cfg(
        {
            "parent": Profile(
                reconcile=ReconcileSpec(
                    plugins=PluginReconcile(policy=ReconcilePolicy.PRUNE),
                ),
            ),
            "child": Profile(
                extends="parent",
                reconcile=ReconcileSpec(
                    plugins=PluginReconcile(policy=ReconcilePolicy.ADDITIVE),
                ),
            ),
        }
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.plugins.policy is ReconcilePolicy.ADDITIVE


def test_resolve_extension_reconcile_inherits() -> None:
    cfg = Config(
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        packages={"x-pkg": ExtensionPackage(extension="x")},
        profiles={
            "parent": Profile(
                reconcile=ReconcileSpec(
                    extensions=ExtensionReconcile(policy=ReconcilePolicy.PRUNE),
                ),
            ),
            "child": Profile(extends="parent", packages=["x-pkg"]),
        },
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.reconcile.extensions.policy is ReconcilePolicy.PRUNE
    assert reconcile_adapter.extensions_input(cfg, resolved).include == ["x"]


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
    assert overlay.disposition == "forked"
