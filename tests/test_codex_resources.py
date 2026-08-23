from hashlib import sha256
from pathlib import Path

import pytest

from setforge.codex_resources import (
    CodexResourceError,
    CodexTomlConflict,
    apply_config_plan,
    capture_toml,
    codex_home,
    compose_fragments,
    expand_filesystem_resources,
    mcp_target_marker,
    plan_config_resources,
    project_is_trusted,
    reconcile_toml,
    resolve_skill,
)
from setforge.config import (
    CodexProfile,
    CodexSkillRef,
    CodexSpec,
    Config,
    Profile,
    resolve_profile,
)


def test_compose_fragments_preserves_distinct_keys() -> None:
    merged, owned = compose_fragments(
        (b'model = "gpt-5"\n', b"[features]\nweb = true\n")
    )
    assert b'model = "gpt-5"' in merged
    assert owned == {("model",), ("features", "web")}


def test_compose_fragments_refuses_duplicate_leaf() -> None:
    with pytest.raises(CodexTomlConflict, match=r"multiple.*model"):
        compose_fragments((b'model = "one"\n', b'model = "two"\n'))


@pytest.mark.parametrize(
    ("first", "second"),
    [(b"[a]\nb = 1\n", b"a = 2\n"), (b"a = 2\n", b"[a]\nb = 1\n")],
)
def test_compose_fragments_refuses_prefix_collisions(
    first: bytes, second: bytes
) -> None:
    with pytest.raises(CodexTomlConflict, match="multiple Codex fragments claim"):
        compose_fragments((first, second))


def test_reconcile_toml_preserves_unmanaged_keys_and_comments() -> None:
    result = reconcile_toml(
        base=b'model = "old"\n',
        live=b'# personal\napproval_policy = "never"\nmodel = "old"\n',
        desired=b'model = "new"\n',
    )
    assert b"# personal" in result
    assert b'approval_policy = "never"' in result
    assert b'model = "new"' in result


def test_reconcile_toml_refuses_same_leaf_conflict() -> None:
    with pytest.raises(CodexTomlConflict, match="both changed model"):
        reconcile_toml(
            base=b'model = "old"\n',
            live=b'model = "local"\n',
            desired=b'model = "upstream"\n',
        )


def test_reconcile_toml_accepts_idempotent_live_value() -> None:
    desired = b'model = "new"\n'
    assert (
        reconcile_toml(base=b'model = "old"\n', live=desired, desired=desired)
        == desired
    )


def test_reconcile_toml_applies_tracked_deletion() -> None:
    assert (
        reconcile_toml(
            base=b'model = "old"\n',
            live=b'model = "old"\napproval_policy = "never"\n',
            desired=b"",
        )
        == b'approval_policy = "never"\n'
    )


def test_reconcile_toml_refuses_local_edit_against_tracked_deletion() -> None:
    with pytest.raises(CodexTomlConflict, match="both changed model"):
        reconcile_toml(base=b'model = "old"\n', live=b'model = "local"\n', desired=b"")


def test_capture_toml_updates_only_managed_keys() -> None:
    captured = capture_toml(
        tracked=b'model = "old"\n',
        live=b'model = "live"\napproval_policy = "never"\n',
    )
    assert captured == b'model = "live"\n'


def test_capture_toml_captures_managed_deletion() -> None:
    assert capture_toml(tracked=b'model = "old"\n', live=b"other = true\n") == b""


def test_malformed_toml_fails_closed() -> None:
    with pytest.raises(CodexResourceError, match="malformed TOML"):
        reconcile_toml(base=None, live=b"[[[", desired=b'model = "new"\n')


def test_codex_home_refuses_relative_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", "relative")
    with pytest.raises(CodexResourceError, match="absolute"):
        codex_home()


def test_project_trust_uses_canonical_project_key(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text(
        f'[projects."{project}"]\ntrust_level = "trusted"\n'
    )
    assert project_is_trusted(project, home=home)


def test_project_trust_rejects_symlinked_config(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "real.toml"
    target.write_text("")
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").symlink_to(target)
    with pytest.raises(CodexResourceError, match="regular file"):
        project_is_trusted(project, home=home)


def test_user_config_fragment_cannot_manage_project_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    (tmp_path / "tracked").mkdir()
    (tmp_path / "tracked/trust.toml").write_text(
        '[projects."/tmp/app"]\ntrust_level = "trusted"\n'
    )
    config = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate({"config": {"trust": {"source": "trust.toml"}}}),
        profiles={"default": Profile(codex=CodexProfile(config=["trust"]))},
    )

    with pytest.raises(CodexResourceError, match="cannot own native project trust"):
        plan_config_resources(
            config,
            resolve_profile(config, "default"),
            tmp_path,
            read_base=lambda _resource_id: None,
        )


def test_skill_destination_refuses_symlinked_scope_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "skills").symlink_to(outside, target_is_directory=True)
    (tmp_path / "tracked/review").mkdir(parents=True)

    with pytest.raises(CodexResourceError, match="symbolic link"):
        resolve_skill("review", CodexSkillRef(source=Path("review")), tmp_path)


def test_expand_user_instructions_and_skills_into_shared_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    (tmp_path / "tracked/codex").mkdir(parents=True)
    (tmp_path / "tracked/codex/AGENTS.md").write_text("instructions")
    (tmp_path / "tracked/codex/review").mkdir()
    (tmp_path / "tracked/codex/review/SKILL.md").write_text("skill")
    config = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "instructions": {"base": {"source": "codex/AGENTS.md"}},
                "skills": {"review": {"source": "codex/review"}},
            }
        ),
        profiles={
            "default": Profile(
                codex=CodexProfile(instructions=["base"], skills=["review"])
            )
        },
    )
    resolved = resolve_profile(config, "default")

    expand_filesystem_resources(config, resolved, tmp_path)

    instruction = config.tracked_files["codex.instruction.base"]
    skill = config.tracked_files["codex.skill.review"]
    assert instruction.dst == str(tmp_path / "home/AGENTS.md")
    assert skill.dst == str(tmp_path / "home/skills/review")
    assert skill.tree is not None
    assert resolved.tracked_files == [
        "codex.instruction.base",
        "codex.skill.review",
    ]


def test_config_plan_freezes_and_applies_managed_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    (tmp_path / "tracked/codex").mkdir(parents=True)
    (tmp_path / "tracked/codex/model.toml").write_text('model = "new"\n')
    (home / "config.toml").write_text(
        '# local\nmodel = "old"\napproval_policy = "never"\n'
    )
    config = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {"config": {"model": {"source": "codex/model.toml"}}}
        ),
        profiles={"default": Profile(codex=CodexProfile(config=["model"]))},
    )
    resolved = resolve_profile(config, "default")
    plans = plan_config_resources(
        config,
        resolved,
        tmp_path,
        read_base=lambda _resource_id: b'model = "old"\n',
    )
    writes: dict[Path, bytes] = {}
    bases: dict[str, bytes] = {}

    changed = apply_config_plan(
        plans[0],
        write=lambda path, data: writes.__setitem__(path, data),
        record_base=lambda resource_id, data: bases.__setitem__(resource_id, data),
    )

    assert changed
    assert b"# local" in writes[home / "config.toml"]
    assert b'approval_policy = "never"' in writes[home / "config.toml"]
    assert b'model = "new"' in writes[home / "config.toml"]
    assert list(bases.values()) == [b'model = "new"\n']
    assert next(iter(bases)).startswith("codex/config/")


def test_mcp_stdio_plan_renders_policy_and_host_environment_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    config = Config(
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "mcp_servers": {
                    "notes": {
                        "transport": "stdio",
                        "command": "uvx",
                        "args": ["notes-mcp"],
                        "cwd": "workspace",
                        "env_vars": ["notes_token"],
                        "enabled": False,
                        "required": True,
                        "startup_timeout_sec": 12,
                        "tool_timeout_sec": 34,
                        "enabled_tools": ["read"],
                        "disabled_tools": ["delete"],
                        "default_tools_approval_mode": "writes",
                        "tools": {"publish": {"approval_mode": "prompt"}},
                    }
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile(mcp_servers=["notes"]))},
    )
    config._codex_environment_vars = {"notes_token": "NOTES_TOKEN"}

    plan = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda _resource_id: None,
    )[0]
    desired = plan.desired.decode()

    assert "[mcp_servers.notes]" in desired
    assert 'command = "uvx"' in desired
    assert 'args = ["notes-mcp"]' in desired
    assert 'cwd = "workspace"' in desired
    assert 'env_vars = ["NOTES_TOKEN"]' in desired
    assert "enabled = false" in desired
    assert "required = true" in desired
    assert "startup_timeout_sec = 12.0" in desired
    assert "tool_timeout_sec = 34.0" in desired
    assert 'enabled_tools = ["read"]' in desired
    assert 'disabled_tools = ["delete"]' in desired
    assert 'default_tools_approval_mode = "writes"' in desired
    assert "[mcp_servers.notes.tools.publish]" in desired
    assert 'approval_mode = "prompt"' in desired


def test_mcp_http_deselection_removes_owned_server_and_preserves_host_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    old = (
        b'[mcp_servers.api]\nurl = "https://old.example.test"\n'
        b'bearer_token_env_var = "API_TOKEN"\nenabled = true\nrequired = false\n'
    )
    live = home / "config.toml"
    live.write_bytes(old + b'\n[mcp_servers.personal]\ncommand = "mine"\n')
    config = Config(
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "mcp_servers": {
                    "api": {
                        "transport": "http",
                        "url": "https://new.example.test",
                        "bearer_token_env_var": "token",
                        "env_http_headers": {"X-Key": "header_key"},
                    }
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile())},
    )

    plan = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda _resource_id: old,
    )[0]

    assert b"mcp_servers.api" not in plan.result
    assert b"mcp_servers.personal" in plan.result
    assert b'command = "mine"' in plan.result


def test_mcp_http_plan_uses_trusted_project_scope_and_host_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    (home / "config.toml").write_text(
        f'[projects."{project}"]\ntrust_level = "trusted"\n'
    )
    config = Config(
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "mcp_servers": {
                    "api": {
                        "transport": "http",
                        "scope": "project",
                        "project": "app",
                        "url": "https://mcp.example.test",
                        "bearer_token_env_var": "token",
                        "env_http_headers": {"X-Key": "header_key"},
                    }
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile(mcp_servers=["api"]))},
    )
    config._codex_project_paths = {"app": project}
    config._codex_environment_vars = {
        "token": "API_TOKEN",
        "header_key": "API_HEADER_KEY",
    }

    plan = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda _resource_id: None,
    )[0]

    assert plan.destination == project / ".codex/config.toml"
    assert b'url = "https://mcp.example.test"' in plan.desired
    assert b'bearer_token_env_var = "API_TOKEN"' in plan.desired
    assert b'X-Key = "API_HEADER_KEY"' in plan.desired


def test_mcp_registry_deletion_retires_marked_prior_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    live = home / "config.toml"
    old = (
        b'[mcp_servers.api]\nurl = "https://mcp.example.test"\n'
        b"enabled = true\nrequired = false\n"
    )
    live.write_bytes(old + b'\n[mcp_servers.personal]\ncommand = "mine"\n')
    marker = mcp_target_marker(live, None)
    config = Config(
        tracked_files={},
        codex=CodexSpec(),
        profiles={"default": Profile(codex=CodexProfile())},
    )

    plan = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda resource_id: (
            old if resource_id.startswith("codex/config/") else b""
        ),
        stored_ids=(marker,),
    )[0]

    assert plan.generated_bytes == ()
    assert plan.mcp_marker_id is None
    assert b"mcp_servers.api" not in plan.result
    assert b"mcp_servers.personal" in plan.result


def test_prior_project_marker_refuses_symlinked_codex_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".codex").symlink_to(outside, target_is_directory=True)
    (home / "config.toml").write_text(
        f'[projects."{project}"]\ntrust_level = "trusted"\n'
    )
    marker = mcp_target_marker(project / ".codex/config.toml", project)
    config = Config(tracked_files={}, profiles={"default": Profile()})

    with pytest.raises(CodexResourceError, match="symbolic link"):
        plan_config_resources(
            config,
            resolve_profile(config, "default"),
            tmp_path,
            read_base=lambda _resource_id: None,
            stored_ids=(marker,),
        )


def test_selected_mcp_plan_records_reversible_destination_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    config = Config(
        tracked_files={},
        codex=CodexSpec.model_validate(
            {"mcp_servers": {"stdio": {"transport": "stdio", "command": "tool"}}}
        ),
        profiles={"default": Profile(codex=CodexProfile(mcp_servers=["stdio"]))},
    )
    plan = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda _resource_id: None,
    )[0]
    markers: dict[str, bytes] = {}

    apply_config_plan(
        plan,
        write=lambda _path, _data: None,
        record_base=lambda _resource_id, _data: None,
        record_marker=lambda resource_id, data: markers.__setitem__(resource_id, data),
    )

    assert markers == {mcp_target_marker(home / "config.toml", None): b""}


def test_selected_mcp_update_reconciles_owned_fields_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    old = b'[mcp_servers.notes]\ncommand = "old"\nenabled = true\nrequired = false\n'
    live = home / "config.toml"
    live.write_bytes(old + b'\n[mcp_servers.personal]\ncommand = "mine"\n')
    config = Config(
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "mcp_servers": {
                    "notes": {
                        "transport": "stdio",
                        "command": "new",
                        "disabled_tools": ["delete"],
                    }
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile(mcp_servers=["notes"]))},
    )

    plan = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda _resource_id: old,
    )[0]

    assert b'command = "new"' in plan.result
    assert b'disabled_tools = ["delete"]' in plan.result
    assert b"mcp_servers.personal" in plan.result
    assert b'command = "mine"' in plan.result


def test_mcp_user_to_project_move_plans_old_retirement_and_new_addition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    user_live = home / "config.toml"
    old = b'[mcp_servers.notes]\ncommand = "tool"\nenabled = true\nrequired = false\n'
    user_live.write_bytes(
        f'[projects."{project}"]\ntrust_level = "trusted"\n'.encode() + old
    )
    config = Config(
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "mcp_servers": {
                    "notes": {
                        "transport": "stdio",
                        "scope": "project",
                        "project": "app",
                        "command": "tool",
                    }
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile(mcp_servers=["notes"]))},
    )
    config._codex_project_paths = {"app": project}
    old_marker = mcp_target_marker(user_live, None)
    old_resource = "codex/config/" + sha256(str(user_live).encode()).hexdigest()[:16]

    plans = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda resource_id: old if resource_id == old_resource else None,
        stored_ids=(old_marker,),
    )
    by_destination = {plan.destination: plan for plan in plans}

    assert b"mcp_servers.notes" not in by_destination[user_live].result
    project_plan = by_destination[project / ".codex/config.toml"]
    assert b"mcp_servers.notes" in project_plan.result
    assert project_plan.mcp_marker_id == mcp_target_marker(
        project / ".codex/config.toml", project
    )


def test_config_plan_identity_is_stable_when_fragment_order_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    (tmp_path / "tracked").mkdir()
    (tmp_path / "tracked/a.toml").write_text("a = 1\n")
    (tmp_path / "tracked/b.toml").write_text("b = 2\n")
    config = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {"config": {"a": {"source": "a.toml"}, "b": {"source": "b.toml"}}}
        ),
        profiles={"default": Profile(codex=CodexProfile(config=["a", "b"]))},
    )
    first = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda _resource_id: None,
    )[0]
    config.profiles["default"].codex = CodexProfile(config=["b", "a"])
    second = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda _resource_id: None,
    )[0]
    assert first.resource_id == second.resource_id
    expected = sha256(str(home / "config.toml").encode()).hexdigest()[:16]
    assert first.resource_id == f"codex/config/{expected}"


def test_config_plan_refuses_live_change_after_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    (tmp_path / "tracked").mkdir()
    (tmp_path / "tracked/model.toml").write_text('model = "new"\n')
    live = home / "config.toml"
    live.write_text('model = "old"\n')
    config = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate({"config": {"model": {"source": "model.toml"}}}),
        profiles={"default": Profile(codex=CodexProfile(config=["model"]))},
    )
    plan = plan_config_resources(
        config,
        resolve_profile(config, "default"),
        tmp_path,
        read_base=lambda _resource_id: b'model = "old"\n',
    )[0]
    live.write_text('model = "raced"\n')

    with pytest.raises(CodexResourceError, match="changed after planning"):
        apply_config_plan(
            plan,
            write=lambda _path, _data: None,
            record_base=lambda _resource_id, _data: None,
        )
