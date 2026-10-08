"""Tests for MCP-server reconcile orchestration.

``subprocess.run`` and binary resolution are monkeypatched so no real
``claude`` CLI is invoked. A :class:`FakeMcpCli` records every argv and
serves a scripted ``mcp get`` registry, letting tests assert the exact
converge behavior (add-absent / update-on-change / ignore-undeclared),
idempotency with readable state, and per-item failure isolation.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from setforge import mcp_servers as mcp
from setforge.config import (
    Config,
    McpScope,
    McpServerRef,
    Profile,
    ResolvedProfile,
    TrackedFile,
)
from setforge.errors import ConfigError, PluginToolMissing, SetforgeError
from tests.fakes import FakeMcpCli


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    """Install a :class:`FakeMcpCli` and stub binary resolution.

    Runs from a non-git temp directory so the local-project key does not depend
    on the git state of the directory pytest was started in.
    """
    monkeypatch.chdir(tmp_path)

    def _install(**kwargs: Any) -> FakeMcpCli:
        cli = FakeMcpCli(**kwargs)
        monkeypatch.setattr(
            "setforge.claude_plugins.resolve_binary", lambda _name: Path("/fake/claude")
        )
        mcp._get_claude_bin.cache_clear()
        monkeypatch.setattr(mcp.subprocess, "run", cli.run)
        return cli

    yield _install
    mcp._get_claude_bin.cache_clear()


def _cfg(servers: dict[str, McpServerRef]) -> Config:
    return Config(
        tracked_files={"d": TrackedFile(src=Path("tracked/x"), dst="~/x")},
        mcp_servers=servers,
        profiles={"default": Profile(tracked_files=["d"], mcp_servers=list(servers))},
    )


def _resolved(names: list[str]) -> ResolvedProfile:
    return ResolvedProfile(mcp_servers=names)


def _reconcile(cfg: Config, profile: ResolvedProfile) -> mcp.McpReconcileReport:
    return mcp.apply_plan(mcp.plan_reconcile(cfg, profile))


# ---------------------------------------------------------------------------
# Schema + cross-ref validation
# ---------------------------------------------------------------------------


def test_mcp_server_ref_rejects_empty_command() -> None:
    with pytest.raises(ValueError, match="non-empty token list"):
        McpServerRef(command=[])


def test_cross_ref_unknown_mcp_name_fails(tmp_path: Path) -> None:
    from setforge.config import load_config

    yaml_text = """
version: 1
tracked_files:
  d: {src: tracked/x, dst: ~/x}
mcp_servers:
  serena: {command: [serena, start-mcp-server]}
profiles:
  default:
    tracked_files: [d]
    mcp_servers: [serena, ghost]
"""
    path = tmp_path / "setforge.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError, match="undeclared server"):
        load_config(path)


def test_cross_ref_all_known_passes(tmp_path: Path) -> None:
    from setforge.config import load_config

    yaml_text = """
version: 1
tracked_files:
  d: {src: tracked/x, dst: ~/x}
mcp_servers:
  serena: {command: [serena, start-mcp-server]}
profiles:
  default:
    tracked_files: [d]
    mcp_servers: [serena]
"""
    path = tmp_path / "setforge.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.mcp_servers["serena"].command == ["serena", "start-mcp-server"]
    assert cfg.mcp_servers["serena"].scope == "user"


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "serena", "args": ["start", 9], "scope": "user"},
        {"command": "serena", "args": ["start"], "scope": "workspace"},
    ],
    ids=["non-string-arg", "unsupported-scope"],
)
def test_mcp_get_command_rejects_malformed_inventory(fake_mcp, payload: object) -> None:
    fake_mcp(
        registry={"serena": (["serena", "start"], "user")},
        get_payloads={"serena": payload},
    )

    with pytest.raises(SetforgeError, match="invalid"):
        mcp.mcp_get_command("serena")


def test_mcp_get_command_treats_os_error_as_unreadable(
    fake_mcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = fake_mcp(registry={"serena": (["serena", "start"], "user")})

    def run(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[2] == "get":
            raise FileNotFoundError(2, "No such file or directory", "/fake/claude")
        return cli.run(argv, **kwargs)

    monkeypatch.setattr(mcp.subprocess, "run", run)

    with pytest.raises(SetforgeError, match="cannot inspect"):
        mcp.mcp_get_command("serena")


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "old", "args": [9], "scope": "user"},
        {"command": "old", "args": [], "scope": "workspace"},
    ],
    ids=["non-string-arg", "unsupported-scope"],
)
def test_converge_does_not_remove_for_malformed_inventory(
    fake_mcp, payload: object
) -> None:
    cli = fake_mcp(
        registry={"serena": (["old"], "user")},
        get_payloads={"serena": payload},
        add_errors={"serena": "already exists"},
    )
    cfg = _cfg({"serena": McpServerRef(command=["new"])})

    with pytest.raises(SetforgeError, match="invalid"):
        _reconcile(cfg, _resolved(["serena"]))
    assert [call[2] for call in cli.calls].count("remove") == 0
    assert cli.registry["serena"] == (["old"], "user")


# ---------------------------------------------------------------------------
# Converge
# ---------------------------------------------------------------------------


def test_converge_adds_absent_server(fake_mcp) -> None:
    cli = fake_mcp(registry={})
    cfg = _cfg({"serena": McpServerRef(command=["serena", "start"])})
    report = _reconcile(cfg, _resolved(["serena"]))
    assert report.added == [("serena", ["serena", "start"], "user")]
    assert report.updated == []
    assert report.failed == []
    assert cli.registry["serena"] == (["serena", "start"], "user")


def test_apply_plan_validates_without_replanning(fake_mcp) -> None:
    cli = fake_mcp(registry={})
    cfg = _cfg({"serena": McpServerRef(command=["serena", "start"])})
    plan = mcp.plan_reconcile(cfg, _resolved(["serena"]))
    get_calls = sum(call[2] == "get" for call in cli.calls)

    mcp.apply_plan(plan)

    assert sum(call[2] == "get" for call in cli.calls) == get_calls + 1
    assert cli.registry["serena"] == (["serena", "start"], "user")


def test_apply_plan_refuses_changed_update_precondition(fake_mcp) -> None:
    cli = fake_mcp(registry={"serena": (["old", "command"], "user")})
    cfg = _cfg({"serena": McpServerRef(command=["new", "command"])})
    plan = mcp.plan_reconcile(cfg, _resolved(["serena"]))
    cli.registry["serena"] = (["changed", "after-plan"], "user")

    report = mcp.apply_plan(plan)

    assert report.updated == []
    assert report.failed == [("serena", "MCP inventory changed after planning")]
    assert cli.registry["serena"] == (["changed", "after-plan"], "user")


def test_apply_plan_replays_unchanged_update(fake_mcp) -> None:
    cli = fake_mcp(registry={"serena": (["old", "command"], "user")})
    cfg = _cfg({"serena": McpServerRef(command=["new", "command"])})
    plan = mcp.plan_reconcile(cfg, _resolved(["serena"]))

    report = mcp.apply_plan(plan)

    assert report.updated == [("serena", ["old", "command"], "user")]
    assert cli.registry["serena"] == (["new", "command"], "user")


def test_validate_plan_covers_declarations_that_were_initially_noops(fake_mcp) -> None:
    cli = fake_mcp(registry={"serena": (["same"], "user")})
    cfg = _cfg({"serena": McpServerRef(command=["same"])})
    plan = mcp.plan_reconcile(cfg, _resolved(["serena"]))
    assert plan.entries == ()
    del cli.registry["serena"]

    with pytest.raises(SetforgeError, match="serena"):
        mcp.validate_plan(plan)


def test_apply_plan_rechecks_declarations_that_were_initially_noops(fake_mcp) -> None:
    cli = fake_mcp(registry={"serena": (["same"], "user")})
    cfg = _cfg({"serena": McpServerRef(command=["same"])})
    plan = mcp.plan_reconcile(cfg, _resolved(["serena"]))
    del cli.registry["serena"]

    report = mcp.apply_plan(plan)

    assert report.failed == [("serena", "MCP inventory changed after planning")]
    assert "serena" not in cli.registry


def test_failed_update_add_still_records_revertible_prior(fake_mcp) -> None:
    cli = fake_mcp(
        registry={"serena": (["old"], "user")},
        add_errors={"serena": "replacement failed"},
    )
    cfg = _cfg({"serena": McpServerRef(command=["new"])})

    report = mcp.apply_plan(mcp.plan_reconcile(cfg, _resolved(["serena"])))

    assert report.updated == [("serena", ["old"], "user")]
    assert report.failed == [("serena", "replacement failed")]
    assert "serena" not in cli.registry


def test_mcp_plan_detaches_mutable_command(fake_mcp) -> None:
    fake_mcp(registry={})
    ref = McpServerRef(command=["before"])
    plan = mcp.plan_reconcile(_cfg({"serena": ref}), _resolved(["serena"]))

    ref.command.append("after")

    assert plan.entries[0].command == ("before",)


def test_add_argv_has_flags_before_name_and_double_dash(fake_mcp) -> None:
    cli = fake_mcp(registry={})
    cfg = _cfg(
        {"serena": McpServerRef(command=["serena", "--port", "9"], scope=McpScope.USER)}
    )
    _reconcile(cfg, _resolved(["serena"]))
    add_call = next(c for c in cli.calls if c[2] == "add")
    assert add_call[:7] == [
        "/fake/claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "serena",
        "--",
    ]
    assert add_call[7:] == ["serena", "--port", "9"]


def test_converge_updates_on_command_change(fake_mcp) -> None:
    cli = fake_mcp(registry={"serena": (["serena", "OLD"], "user")})
    cfg = _cfg({"serena": McpServerRef(command=["serena", "NEW"])})
    report = _reconcile(cfg, _resolved(["serena"]))
    assert report.added == [("serena", ["serena", "NEW"], "user")]
    assert report.updated == [("serena", ["serena", "OLD"], "user")]
    assert cli.registry["serena"] == (["serena", "NEW"], "user")
    # remove + re-add happened.
    verbs = [c[2] for c in cli.calls]
    assert "remove" in verbs
    assert "add" in verbs


def test_converge_noop_when_command_matches(fake_mcp) -> None:
    cli = fake_mcp(registry={"serena": (["serena", "start"], "user")})
    cfg = _cfg({"serena": McpServerRef(command=["serena", "start"])})
    report = _reconcile(cfg, _resolved(["serena"]))
    assert report.added == []
    assert report.updated == []
    assert report.failed == []
    # Planning and apply-time validation each probe; neither mutates.
    assert [c[2] for c in cli.calls] == ["get", "get"]


def test_converge_ignores_undeclared_servers(fake_mcp) -> None:
    cli = fake_mcp(registry={"handmade": (["hand"], "user")})
    cfg = _cfg({"serena": McpServerRef(command=["serena"])})
    _reconcile(cfg, _resolved(["serena"]))
    # The undeclared server is left untouched.
    assert cli.registry["handmade"] == (["hand"], "user")
    assert "handmade" not in {c[5] for c in cli.calls if c[2] == "remove"}


# ---------------------------------------------------------------------------
# Idempotency + per-item failure
# ---------------------------------------------------------------------------


def test_already_exists_without_known_command_is_unverifiable(fake_mcp) -> None:
    # get returns absent (so we attempt add), but add says already exists.
    fake_mcp(
        registry={},
        add_errors={"serena": "Error: server 'serena' already exists"},
    )
    cfg = _cfg({"serena": McpServerRef(command=["serena"])})
    report = _reconcile(cfg, _resolved(["serena"]))
    assert [name for name, _detail in report.failed] == ["serena"]
    assert "cannot verify" in report.failed[0][1]
    assert report.added == []  # not counted as a fresh add


def test_install_reports_unverifiable_server_and_preserves_successful_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_mcp
) -> None:
    from typer.testing import CliRunner

    from setforge.cli import app

    run_subprocess = subprocess.run
    cli = fake_mcp(
        registry={
            "existing": (["server", "old exact argument"], "user"),
            "handmade": (["unrelated"], "user"),
        },
        add_errors={"existing": "MCP server existing already exists in user config"},
    )

    def run(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[0] != "/fake/claude":
            return run_subprocess(argv, **kwargs)
        if argv[1:3] == ["mcp", "get"]:
            cli.calls.append(list(argv))
            # Captured from Claude Code 2.1.219 in an isolated home.
            raise subprocess.CalledProcessError(
                1, argv, stderr="error: unknown option '--json'\n"
            )
        return cli.run(argv, **kwargs)

    monkeypatch.setattr(mcp.subprocess, "run", run)
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "setforge.yaml"
    config.write_text(
        """\
version: 1
schema_version: '6.5'
tracked_files: {}
mcp_servers:
  existing:
    command: [server, new exact argument]
  fresh:
    command: [new-server, argument with spaces]
profiles:
  p:
    mcp_servers: [existing, fresh]
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--no-fetch",
            "--no-git-check",
            "--yes",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "cannot verify" in result.output
    assert cli.registry == {
        "existing": (["server", "old exact argument"], "user"),
        "handmade": (["unrelated"], "user"),
        "fresh": (["new-server", "argument with spaces"], "user"),
    }
    assert not any(call[2] == "remove" for call in cli.calls)
    deltas = list((tmp_path / "state").rglob("mcp.json"))
    assert len(deltas) == 1
    payload = json.loads(deltas[0].read_text())
    assert payload.pop("context") == list(mcp.inventory_context())
    assert payload == {
        "added": [["fresh", ["new-server", "argument with spaces"], "user"]],
        "updated": [],
    }


def test_per_item_failure_does_not_abort_loop(fake_mcp) -> None:
    cli = fake_mcp(
        registry={},
        add_errors={"bad": "boom: spawn ENOENT"},
    )
    cfg = _cfg(
        {
            "bad": McpServerRef(command=["bad"]),
            "good": McpServerRef(command=["good"]),
        }
    )
    report = _reconcile(cfg, _resolved(["bad", "good"]))
    assert ("bad", "boom: spawn ENOENT") in report.failed
    assert report.added == [("good", ["good"], "user")]
    assert cli.registry["good"] == (["good"], "user")


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "No such file or directory", "/fake/claude"),
        PermissionError(13, "Permission denied", "/fake/claude"),
    ],
    ids=["file-not-found", "permission-denied"],
)
def test_os_error_during_add_is_reported_and_does_not_abort_loop(
    fake_mcp, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    cli = fake_mcp(registry={})

    def run(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[2] == "add" and argv[5] == "bad":
            raise error
        return cli.run(argv, **kwargs)

    monkeypatch.setattr(mcp.subprocess, "run", run)
    cfg = _cfg(
        {
            "bad": McpServerRef(command=["bad"]),
            "good": McpServerRef(command=["good"]),
        }
    )

    report = _reconcile(cfg, _resolved(["bad", "good"]))

    assert report.failed == [("bad", str(error))]
    assert report.added == [("good", ["good"], "user")]
    assert cli.registry["good"] == (["good"], "user")


def test_os_error_during_remove_is_reported_and_does_not_abort_loop(
    fake_mcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = fake_mcp(registry={"bad": (["old"], "user")})
    error = PermissionError(13, "Permission denied", "/fake/claude")

    def run(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[2] == "remove" and argv[5] == "bad":
            raise error
        return cli.run(argv, **kwargs)

    monkeypatch.setattr(mcp.subprocess, "run", run)
    cfg = _cfg(
        {
            "bad": McpServerRef(command=["new"]),
            "good": McpServerRef(command=["good"]),
        }
    )

    report = _reconcile(cfg, _resolved(["bad", "good"]))

    assert report.updated == []
    assert report.failed == [("bad", str(error))]
    assert report.added == [("good", ["good"], "user")]
    assert cli.registry["bad"] == (["old"], "user")
    assert cli.registry["good"] == (["good"], "user")


def test_undeclared_profile_name_raises(fake_mcp) -> None:
    fake_mcp(registry={})
    cfg = _cfg({"serena": McpServerRef(command=["serena"])})
    with pytest.raises(ConfigError, match="undeclared MCP server"):
        _reconcile(cfg, _resolved(["ghost"]))


def test_missing_claude_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("setforge.claude_plugins.resolve_binary", lambda _name: None)
    mcp._get_claude_bin.cache_clear()
    with pytest.raises(PluginToolMissing):
        mcp.ensure_claude_available()
    mcp._get_claude_bin.cache_clear()


@pytest.fixture
def native_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_mcp
) -> tuple[FakeMcpCli, Path, Path]:
    cli = fake_mcp()
    cwd = tmp_path / "native-project"
    cwd.mkdir()
    config = tmp_path / "native-config" / ".claude.json"
    config.parent.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config.parent))

    def run(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[:3] == ["/fake/claude", "mcp", "get"]:
            raise subprocess.CalledProcessError(
                1, argv, stderr="error: unknown option '--json'\n"
            )
        return cli.run(argv, **kwargs)

    monkeypatch.setattr(mcp.subprocess, "run", run)
    return cli, cwd, config


@pytest.mark.parametrize("scope", ["user", "local", "project"])
def test_native_inventory_preserves_exact_scope_tokens_and_override(
    native_inventory, scope: str
) -> None:
    _cli, cwd, config = native_inventory
    command = ["server", "argument with spaces", 'quote"', "slash\\", "--flag"]
    server = {"type": "stdio", "command": command[0], "args": command[1:], "env": {}}
    if scope == "project":
        (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"named": server}}))
    elif scope == "local":
        config.write_text(
            json.dumps({"projects": {str(cwd): {"mcpServers": {"named": server}}}})
        )
    else:
        config.write_text(json.dumps({"mcpServers": {"named": server}}))
    before = {p: p.read_bytes() for p in (config, cwd / ".mcp.json") if p.exists()}
    assert mcp.mcp_get_command("named") == (command, scope)
    assert mcp.mcp_get_command("absent") is None
    assert {p: p.read_bytes() for p in before} == before
    assert not (Path.home() / ".claude.json").exists()


def test_native_local_inventory_uses_git_root_but_project_inventory_uses_cwd(
    native_inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, root, config = native_inventory
    cli.real_run(["git", "init", str(root)], check=True, capture_output=True)
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    local = {"command": "local-server", "args": ["local exact"]}
    project = {"command": "project-server", "args": ["project exact"]}
    config.write_text(
        json.dumps({"projects": {str(root): {"mcpServers": {"local": local}}}})
    )
    (nested / ".mcp.json").write_text(json.dumps({"mcpServers": {"project": project}}))
    assert mcp.mcp_get_command("local") == (["local-server", "local exact"], "local")
    assert mcp.mcp_get_command("project") == (
        ["project-server", "project exact"],
        "project",
    )


@pytest.mark.parametrize(
    "body",
    [
        '{"mcpServers": {"named": {"command":"x", "args":[9]}}}',
        '{"mcpServers": {"named": {"command":""}}}',
        '{"mcpServers": {"named": {"command":"x", "env":{"KEEP":"v"}}}}',
        '{"mcpServers": {"named": {"type":"http", "url":"https://example.test"}}}',
        '{"mcpServers": {"named": {"command":"x", "unknown":"v"}}}',
        '{"mcpServers": []}',
        '{"projects": []}',
        '{"mcpServers": {"named":{"command":"x"}, "named":{"command":"y"}}}',
        "{broken",
        "[]",
    ],
)
def test_native_inventory_refuses_invalid_or_unrepresentable_state(
    native_inventory, body: str
) -> None:
    cli, _cwd, config = native_inventory
    config.write_text(body)
    before = config.read_bytes()
    with pytest.raises(SetforgeError):
        mcp.plan_reconcile(
            _cfg({"named": McpServerRef(command=["new"])}), _resolved(["named"])
        )
    assert config.read_bytes() == before
    assert not any(call[2] in {"add", "remove"} for call in cli.calls)


def test_native_inventory_refuses_shadowed_same_name(native_inventory) -> None:
    _cli, cwd, config = native_inventory
    config.write_text(json.dumps({"mcpServers": {"named": {"command": "user"}}}))
    (cwd / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"named": {"command": "project"}}})
    )
    with pytest.raises(SetforgeError, match="ambiguous"):
        mcp.mcp_get_command("named")


def test_unknown_git_discovery_refuses_inventory(
    native_inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli, _cwd, _config = native_inventory
    monkeypatch.setattr(
        mcp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 128, stdout="", stderr="fatal: bad git configuration"
        ),
    )
    with pytest.raises(SetforgeError, match="local-project key"):
        mcp.inventory_context()


def test_mcp_plan_refuses_changed_native_context(
    fake_mcp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = fake_mcp(registry={"named": (["old"], "user")})
    plan = mcp.plan_reconcile(
        _cfg({"named": McpServerRef(command=["new"])}), _resolved(["named"])
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with pytest.raises(SetforgeError, match="context changed"):
        mcp.apply_plan(plan)
    assert cli.registry == {"named": (["old"], "user")}
    assert not any(call[2] in {"add", "remove"} for call in cli.calls)


@pytest.mark.parametrize("extra", [{"cwd": "/unmodeled-cwd"}, {"unknown": "state"}])
def test_public_install_refuses_unrepresentable_json_receipt_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_mcp, extra: dict[str, str]
) -> None:
    from typer.testing import CliRunner

    from setforge import locking, transitions
    from setforge.cli import app
    from setforge.ownership import OwnershipStore

    real_run = subprocess.run
    cli = fake_mcp(
        registry={"named": (["old"], "user"), "control": (["keep"], "user")},
        get_payloads={
            "named": {"command": "old", "args": [], "scope": "user", **extra}
        },
    )

    def run(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        return (
            cli.run(argv, **kwargs)
            if argv[0] == "/fake/claude"
            else real_run(argv, **kwargs)
        )

    monkeypatch.setattr(mcp.subprocess, "run", run)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "native-config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    tracked = tmp_path / "tracked"
    tracked.mkdir()
    (tracked / "note.md").write_text("ordinary fixture\n")
    live = tmp_path / "live"
    config = tmp_path / "setforge.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "schema_version": "6.5",
                "tracked_files": {"note": {"src": "note.md", "dst": str(live)}},
                "mcp_servers": {"named": {"command": ["new"]}},
                "profiles": {
                    "p": {"tracked_files": ["note"], "mcp_servers": ["named"]}
                },
            }
        )
    )
    for path in (
        transitions.state_root(),
        locking._user_global_locks_dir(),
        OwnershipStore().root,
    ):
        assert path.resolve().is_relative_to(tmp_path.resolve())
    before = dict(cli.registry)
    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--no-fetch",
            "--no-git-check",
            "--yes",
        ],
    )
    assert result.exit_code != 0, (result.output, result.exception)
    assert "cannot represent" in str(result.exception)
    assert cli.registry == before
    assert not live.exists()
    assert not any(call[2] in {"add", "remove"} for call in cli.calls)


def test_successful_update_announces_once_and_retains_both_endpoints(
    fake_mcp, capsys: pytest.CaptureFixture[str]
) -> None:
    from setforge.cli._mcp_helpers import reconcile_mcp_servers

    fake_mcp(registry={"named": (["old"], "user")})
    cfg = _cfg(
        {
            "named": McpServerRef(command=["new"]),
            "fresh": McpServerRef(command=["fresh"]),
        }
    )
    delta, failures = reconcile_mcp_servers(cfg, _resolved(["named", "fresh"]))
    assert failures == []
    assert delta is not None
    assert ("named", ("new",), "user") in delta.added
    assert delta.updated == (("named", ("old",), "user"),)
    output = capsys.readouterr().out
    assert "mcp added     fresh" in output
    assert "mcp added     named" not in output
    assert (
        sum(
            line.startswith("mcp updated") and line.endswith(" named")
            for line in output.splitlines()
        )
        == 1
    )


@pytest.mark.parametrize("claude_available", [False, True])
def test_missing_claude_skips_before_git_but_available_claude_requires_context(
    fake_mcp,
    monkeypatch: pytest.MonkeyPatch,
    claude_available: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from setforge.cli._mcp_helpers import plan_mcp_servers

    fake_mcp()
    if not claude_available:
        monkeypatch.setattr(
            "setforge.claude_plugins.resolve_binary", lambda _name: None
        )
    probes: list[list[str]] = []

    def missing_git(argv, **kwargs: Any) -> subprocess.CompletedProcess:
        probes.append(list(argv))
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(mcp.subprocess, "run", missing_git)
    cfg = _cfg({"named": McpServerRef(command=["server"])})
    if claude_available:
        with pytest.raises(SetforgeError, match="local-project key"):
            plan_mcp_servers(cfg, _resolved(["named"]))
        assert probes
    else:
        assert plan_mcp_servers(cfg, _resolved(["named"])).value is None
        assert probes == []
        assert "skipping MCP server reconcile" in capsys.readouterr().err


def test_empty_mcp_plan_needs_no_native_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp,
        "ensure_claude_available",
        lambda: pytest.fail("empty MCP work probed Claude"),
    )
    monkeypatch.setattr(
        mcp, "inventory_context", lambda: pytest.fail("empty MCP work probed Git")
    )
    assert mcp.plan_reconcile(_cfg({}), _resolved([])) == mcp.McpPlan(
        entries=(), preconditions=()
    )


def _context_moved_to(
    monkeypatch: pytest.MonkeyPatch, *, cwd: str, config: str, key: str
) -> tuple[str, str, str]:
    recorded = ("/work/a", "/home/u/.claude.json", "/work/a")
    monkeypatch.setattr(mcp, "inventory_context", lambda: (cwd, config, key))
    return recorded


def test_user_scope_context_ignores_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _context_moved_to(
        monkeypatch, cwd="/work/b", config="/home/u/.claude.json", key="/work/b"
    )

    mcp.require_inventory_context(recorded, ["user"])

    with pytest.raises(SetforgeError, match="context changed"):
        mcp.require_inventory_context(recorded)


def test_user_scope_context_still_requires_same_config_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _context_moved_to(
        monkeypatch, cwd="/work/a", config="/other/.claude.json", key="/work/a"
    )

    with pytest.raises(SetforgeError, match="context changed"):
        mcp.require_inventory_context(recorded, ["user"])


def test_local_and_project_scope_context_track_their_locations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _context_moved_to(
        monkeypatch, cwd="/work/a", config="/home/u/.claude.json", key="/work/b"
    )
    with pytest.raises(SetforgeError, match="context changed"):
        mcp.require_inventory_context(recorded, ["user", "local"])
    mcp.require_inventory_context(recorded, ["project"])

    recorded = _context_moved_to(
        monkeypatch, cwd="/work/b", config="/home/u/.claude.json", key="/work/a"
    )
    with pytest.raises(SetforgeError, match="context changed"):
        mcp.require_inventory_context(recorded, ["project"])
    mcp.require_inventory_context(recorded, ["user"])
