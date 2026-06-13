"""Tests for MCP-server reconcile orchestration.

``subprocess.run`` and binary resolution are monkeypatched so no real
``claude`` CLI is invoked. A :class:`FakeMcpCli` records every argv and
serves a scripted ``mcp get`` registry, letting tests assert the exact
converge behavior (add-absent / update-on-change / ignore-undeclared),
idempotency ("already exists" swallow), and per-item failure isolation.
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
    McpServerRef,
    Profile,
    ResolvedProfile,
    TrackedFile,
)
from setforge.errors import ConfigError, PluginToolMissing


class FakeMcpCli:
    """Scripted ``claude mcp`` driver recording argv lists.

    ``registry`` maps name -> (command_tokens, scope) and models the live
    server state. ``add``/``remove`` mutate it; ``get --json`` reads it.
    ``add_errors`` / ``remove_errors`` map a name -> stderr string to raise
    a :class:`subprocess.CalledProcessError` for that op.
    """

    def __init__(
        self,
        *,
        registry: dict[str, tuple[list[str], str]] | None = None,
        add_errors: dict[str, str] | None = None,
        remove_errors: dict[str, str] | None = None,
    ) -> None:
        self.registry = registry or {}
        self.add_errors = add_errors or {}
        self.remove_errors = remove_errors or {}
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        # argv[0] is the claude binary; argv[1] == "mcp".
        verb = argv[2]
        if verb == "get":
            name = argv[3]
            if name not in self.registry:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="No MCP server found"
                )
            command, scope = self.registry[name]
            payload = {
                "command": command[0],
                "args": command[1:],
                "scope": scope,
            }
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload))
        if verb == "add":
            # [claude, mcp, add, --scope, <scope>, <name>, --, *tokens]
            scope = argv[4]
            name = argv[5]
            assert argv[6] == "--", f"expected literal -- separator, got {argv[6]!r}"
            tokens = list(argv[7:])
            if name in self.add_errors:
                raise subprocess.CalledProcessError(
                    1, argv, stderr=self.add_errors[name]
                )
            self.registry[name] = (tokens, scope)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        if verb == "remove":
            # [claude, mcp, remove, --scope, <scope>, <name>]
            name = argv[5]
            if name in self.remove_errors:
                raise subprocess.CalledProcessError(
                    1, argv, stderr=self.remove_errors[name]
                )
            self.registry.pop(name, None)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise AssertionError(f"unexpected mcp verb {verb!r}")


@pytest.fixture
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Install a :class:`FakeMcpCli` and stub binary resolution."""

    def _install(**kwargs: Any) -> FakeMcpCli:
        cli = FakeMcpCli(**kwargs)
        monkeypatch.setattr(mcp, "resolve_binary", lambda _name: Path("/fake/claude"))
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


# ---------------------------------------------------------------------------
# Converge
# ---------------------------------------------------------------------------


def test_converge_adds_absent_server(fake_mcp) -> None:
    cli = fake_mcp(registry={})
    cfg = _cfg({"serena": McpServerRef(command=["serena", "start"])})
    report = mcp.reconcile(cfg, _resolved(["serena"]))
    assert report.added == [("serena", ["serena", "start"], "user")]
    assert report.updated == []
    assert report.failed == []
    assert cli.registry["serena"] == (["serena", "start"], "user")


def test_add_argv_has_flags_before_name_and_double_dash(fake_mcp) -> None:
    cli = fake_mcp(registry={})
    cfg = _cfg(
        {"serena": McpServerRef(command=["serena", "--port", "9"], scope="user")}
    )
    mcp.reconcile(cfg, _resolved(["serena"]))
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
    report = mcp.reconcile(cfg, _resolved(["serena"]))
    assert report.added == []
    assert report.updated == [("serena", ["serena", "OLD"], "user")]
    assert cli.registry["serena"] == (["serena", "NEW"], "user")
    # remove + re-add happened.
    verbs = [c[2] for c in cli.calls]
    assert "remove" in verbs
    assert "add" in verbs


def test_converge_noop_when_command_matches(fake_mcp) -> None:
    cli = fake_mcp(registry={"serena": (["serena", "start"], "user")})
    cfg = _cfg({"serena": McpServerRef(command=["serena", "start"])})
    report = mcp.reconcile(cfg, _resolved(["serena"]))
    assert report.added == []
    assert report.updated == []
    assert report.failed == []
    # Only the `get` probe ran — no add/remove.
    assert [c[2] for c in cli.calls] == ["get"]


def test_converge_ignores_undeclared_servers(fake_mcp) -> None:
    cli = fake_mcp(registry={"handmade": (["hand"], "user")})
    cfg = _cfg({"serena": McpServerRef(command=["serena"])})
    mcp.reconcile(cfg, _resolved(["serena"]))
    # The undeclared server is left untouched.
    assert cli.registry["handmade"] == (["hand"], "user")
    assert "handmade" not in {c[5] for c in cli.calls if c[2] == "remove"}


# ---------------------------------------------------------------------------
# Idempotency + per-item failure
# ---------------------------------------------------------------------------


def test_already_exists_stderr_is_swallowed(fake_mcp) -> None:
    # get returns absent (so we attempt add), but add says already exists.
    fake_mcp(
        registry={},
        add_errors={"serena": "Error: server 'serena' already exists"},
    )
    cfg = _cfg({"serena": McpServerRef(command=["serena"])})
    report = mcp.reconcile(cfg, _resolved(["serena"]))
    assert report.failed == []
    assert report.added == []  # not counted as a fresh add


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
    report = mcp.reconcile(cfg, _resolved(["bad", "good"]))
    assert ("bad", "boom: spawn ENOENT") in report.failed
    assert report.added == [("good", ["good"], "user")]
    assert cli.registry["good"] == (["good"], "user")


def test_undeclared_profile_name_raises(fake_mcp) -> None:
    fake_mcp(registry={})
    cfg = _cfg({"serena": McpServerRef(command=["serena"])})
    with pytest.raises(ConfigError, match="undeclared MCP server"):
        mcp.reconcile(cfg, _resolved(["ghost"]))


def test_missing_claude_binary_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp, "resolve_binary", lambda _name: None)
    mcp._get_claude_bin.cache_clear()
    with pytest.raises(PluginToolMissing):
        mcp.ensure_claude_available()
    mcp._get_claude_bin.cache_clear()
