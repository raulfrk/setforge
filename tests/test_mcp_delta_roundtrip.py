"""MCPDelta transition round-trip + reverse-helper tests.

Covers the on-disk serialization (omit-when-empty, JSON-primitive
shape), the :func:`mcp_delta_from_json` boundary guard, and the
install -> revert -> revert (redo) round-trip through ``_reverse_mcp``
with a mocked ``claude mcp`` CLI.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from setforge import mcp_servers as mcp
from setforge import transitions
from setforge.cli import _mcp_helpers
from setforge.errors import InvalidTransitionRecord

# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


def test_empty_delta_omits_mcp_json() -> None:
    empty = transitions.MCPDelta(added=(), updated=())
    assert empty.is_empty()
    assert transitions._serialize_mcp_payload(empty) is None
    assert transitions._serialize_mcp_payload(None) is None


def test_delta_roundtrip_added_and_updated() -> None:
    delta = transitions.MCPDelta(
        added=(("serena", ("serena", "start"), "user"),),
        updated=(("ctx", ("ctx", "OLD"), "project"),),
    )
    payload = transitions._serialize_mcp_payload(delta)
    assert payload is not None
    raw = json.loads(payload)
    back = transitions.mcp_delta_from_json(raw)
    assert back == delta


def test_from_json_rejects_malformed_entry() -> None:
    with pytest.raises(InvalidTransitionRecord, match="malformed added entry"):
        transitions.mcp_delta_from_json({"added": [["serena", ["x"]]]})


def test_from_json_rejects_non_list_command() -> None:
    with pytest.raises(InvalidTransitionRecord):
        transitions.mcp_delta_from_json({"added": [["serena", "notalist", "user"]]})


def test_write_transition_stages_mcp_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(transitions, "transitions_root", lambda: tmp_path / "tr")
    meta = transitions.make_meta(transitions.TransitionCommand.INSTALL, "p")
    delta = transitions.MCPDelta(
        added=(("serena", ("serena", "start"), "user"),), updated=()
    )
    target = transitions.write_transition(meta, {}, {}, None, mcp_delta=delta)
    mcp_file = target / "mcp.json"
    assert mcp_file.exists()
    raw = json.loads(mcp_file.read_text())
    assert transitions.mcp_delta_from_json(raw) == delta


def test_write_transition_no_mcp_json_when_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(transitions, "transitions_root", lambda: tmp_path / "tr")
    meta = transitions.make_meta(transitions.TransitionCommand.INSTALL, "p")
    target = transitions.write_transition(
        meta, {}, {}, None, mcp_delta=transitions.MCPDelta(added=(), updated=())
    )
    assert not (target / "mcp.json").exists()


# ---------------------------------------------------------------------------
# Reverse helper round-trip
# ---------------------------------------------------------------------------


class FakeMcpCli:
    """Minimal ``claude mcp add/remove`` recorder for reverse tests."""

    def __init__(self) -> None:
        self.real_run = subprocess.run
        self.registry: dict[str, tuple[list[str], str]] = {}
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs: Any) -> subprocess.CompletedProcess:
        if argv[0] == "git":
            return self.real_run(argv, **kwargs)
        self.calls.append(list(argv))
        verb = argv[2]
        if verb == "get":
            current = self.registry.get(argv[3])
            if current is None:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="No MCP server found"
                )
            command, scope = current
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {"command": command[0], "args": command[1:], "scope": scope}
                ),
            )
        if verb == "add":
            scope, name = argv[4], argv[5]
            self.registry[name] = (list(argv[7:]), scope)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        if verb == "remove":
            self.registry.pop(argv[5], None)
            return subprocess.CompletedProcess(argv, 0, stdout="")
        raise AssertionError(verb)


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeMcpCli]:
    cli = FakeMcpCli()
    monkeypatch.setattr(mcp, "resolve_binary", lambda _name: Path("/fake/claude"))
    mcp._get_claude_bin.cache_clear()
    monkeypatch.setattr(mcp.subprocess, "run", cli.run)
    yield cli
    mcp._get_claude_bin.cache_clear()


def test_reverse_added_removes_then_redo_readds(fake_cli: FakeMcpCli) -> None:
    # Simulate install having added serena.
    fake_cli.registry["serena"] = (["serena", "start"], "user")
    forward = transitions.MCPDelta(
        added=(("serena", ("serena", "start"), "user"),),
        updated=(),
        context=mcp.inventory_context(),
    )

    # Revert: removes serena, returns a reverse delta.
    reverse, failed = _mcp_helpers._reverse_mcp(forward)
    assert failed == []
    assert "serena" not in fake_cli.registry
    assert reverse is not None
    # The removal is recorded under ``updated`` so the redo re-adds it.
    assert reverse.updated == (("serena", ("serena", "start"), "user"),)
    assert reverse.added == ()

    # Redo: revert the reverse delta -> re-adds serena exactly.
    redo, failed2 = _mcp_helpers._reverse_mcp(reverse)
    assert failed2 == []
    assert fake_cli.registry["serena"] == (["serena", "start"], "user")
    assert redo is not None


def test_reverse_updated_readds_prior_command(fake_cli: FakeMcpCli) -> None:
    # Install changed serena from OLD -> NEW; live now has NEW.
    fake_cli.registry["serena"] = (["serena", "NEW"], "user")
    forward = transitions.MCPDelta(
        added=(("serena", ("serena", "NEW"), "user"),),
        updated=(("serena", ("serena", "OLD"), "user"),),
        context=mcp.inventory_context(),
    )
    reverse, failed = _mcp_helpers._reverse_mcp(forward)
    assert failed == []
    # Prior command restored.
    assert fake_cli.registry["serena"] == (["serena", "OLD"], "user")
    assert reverse is not None
    # The re-add is recorded under ``added`` so the redo removes it again.
    assert reverse.added == (("serena", ("serena", "OLD"), "user"),)
    assert reverse.updated == (("serena", ("serena", "NEW"), "user"),)
    redo, failed = _mcp_helpers._reverse_mcp(reverse)
    assert failed == []
    assert redo is not None
    assert fake_cli.registry["serena"] == (["serena", "NEW"], "user")


def test_reverse_empty_delta_returns_none(fake_cli: FakeMcpCli) -> None:
    reverse, failed = _mcp_helpers._reverse_mcp(
        transitions.MCPDelta(added=(), updated=())
    )
    assert reverse is None
    assert failed == []


@pytest.mark.parametrize("boundary", ["legacy", "stale", "context"])
def test_reverse_refuses_unproved_registration_without_effects(
    fake_cli: FakeMcpCli, boundary: str
) -> None:
    context = mcp.inventory_context()
    fake_cli.registry["named"] = (["unexpected"], "project")
    fake_cli.registry["control"] = (["keep"], "user")
    if boundary == "context":
        context = ("/other-project", context[1], "/other-project")
    delta = transitions.MCPDelta(
        added=(("named", ("new",), "project"),),
        updated=(("named", ("old",), "user"),),
        context=None if boundary == "legacy" else context,
    )
    before = dict(fake_cli.registry)
    reverse, failures = _mcp_helpers._reverse_mcp(delta)
    assert reverse is None
    assert failures
    assert fake_cli.registry == before
    assert not any(call[2] in {"add", "remove"} for call in fake_cli.calls)


def test_removed_prior_only_delta_restores_exact_endpoint(fake_cli: FakeMcpCli) -> None:
    delta = transitions.MCPDelta(
        added=(),
        updated=(("named", ("old", "arg with spaces"), "local"),),
        context=mcp.inventory_context(),
    )
    reverse, failed = _mcp_helpers._reverse_mcp(delta)
    assert failed == []
    assert reverse is not None
    assert fake_cli.registry == {"named": (["old", "arg with spaces"], "local")}
    assert reverse.updated == ()


def test_context_metadata_roundtrips_and_legacy_metadata_decodes() -> None:
    delta = transitions.MCPDelta(
        added=(("named", ("exact",), "project"),),
        updated=(),
        context=mcp.inventory_context(),
    )
    payload = transitions._serialize_mcp_payload(delta)
    assert payload is not None
    raw = json.loads(payload)
    assert transitions.mcp_delta_from_json(raw) == delta
    raw.pop("context")
    assert transitions.mcp_delta_from_json(raw).context is None


@pytest.mark.parametrize(
    "context",
    [
        None,
        [],
        ["/cwd", "/config"],
        ["relative", "/config", "/local"],
        ["/cwd/../other", "/config", "/local"],
    ],
)
def test_context_boundary_rejects_invalid_metadata(context: object) -> None:
    with pytest.raises(InvalidTransitionRecord, match="context"):
        transitions.mcp_delta_from_json(
            {"added": [], "updated": [], "context": context}
        )
