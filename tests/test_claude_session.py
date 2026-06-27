"""Unit tests for :mod:`setforge.claude_session`.

The real ``claude`` is faked at the subprocess boundary by the shared
``claude_stub`` fixture (a stub executable resolved through
``SETFORGE_CLAUDE_BIN`` that records argv + stdin and emits scripted output). No
network, no real CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.claude_session import ClaudeSession, ClaudeSessionError
from tests.conftest import ClaudeStub

# --------------------------------------------------------------------------- #
# Happy path — argv / stdin shape
# --------------------------------------------------------------------------- #


def test_first_turn_uses_session_id_and_stdin(claude_stub: ClaudeStub) -> None:
    claude_stub.set(STUB_STDOUT="draft one\n")
    out = ClaudeSession().send("PROMPT BODY")
    assert out == "draft one\n"
    (call,) = claude_stub.calls()
    assert call["stdin"] == "PROMPT BODY"  # prompt on stdin, never argv
    assert "PROMPT BODY" not in call["argv"]
    assert "--session-id" in call["argv"]
    assert "--resume" not in call["argv"]
    # The preset id rides turn 1 and is a 32-char uuid4 hex.
    sid = call["argv"][call["argv"].index("--session-id") + 1]
    assert len(sid) == 32


def test_tools_disabled_and_budget_capped(claude_stub: ClaudeStub) -> None:
    ClaudeSession().send("x")
    (call,) = claude_stub.calls()
    argv = call["argv"]
    # --tools "" disables all tools; the value is the empty string.
    assert argv[argv.index("--tools") + 1] == ""
    assert "--max-budget-usd" in argv
    assert "-p" in argv
    # No --model: inherit the user's default.
    assert "--model" not in argv


def test_resume_reuses_same_session_id(claude_stub: ClaudeStub) -> None:
    session = ClaudeSession()
    session.send("first")
    session.send("second")
    first, second = claude_stub.calls()
    sid1 = first["argv"][first["argv"].index("--session-id") + 1]
    assert "--resume" in second["argv"]
    sid2 = second["argv"][second["argv"].index("--resume") + 1]
    assert sid2 == sid1  # the resume turn carries the SAME id
    assert "--session-id" not in second["argv"]


# --------------------------------------------------------------------------- #
# Failure → ClaudeSessionError (all re-promptable)
# --------------------------------------------------------------------------- #


def test_nonzero_exit_raises_with_stderr_tail(claude_stub: ClaudeStub) -> None:
    claude_stub.set(STUB_EXIT=3, STUB_STDERR="boom: model error\n")
    with pytest.raises(ClaudeSessionError, match="boom: model error"):
        ClaudeSession().send("x")


def test_empty_output_raises(claude_stub: ClaudeStub) -> None:
    claude_stub.set(STUB_STDOUT="   \n")
    with pytest.raises(ClaudeSessionError, match="empty"):
        ClaudeSession().send("x")


def test_undecodable_output_raises(claude_stub: ClaudeStub) -> None:
    claude_stub.set(STUB_RAW_STDOUT="1")
    with pytest.raises(ClaudeSessionError, match="non-UTF-8"):
        ClaudeSession().send("x")


def test_timeout_raises(
    claude_stub: ClaudeStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("setforge.claude_session._TIMEOUT_S", 0.3)
    claude_stub.set(STUB_SLEEP=5)
    with pytest.raises(ClaudeSessionError, match="timed out"):
        ClaudeSession().send("x")


def test_missing_binary_degrades_not_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An override pointing at a nonexistent path → BinaryOverrideInvalid inside
    # resolve_binary, which A4 wraps as ClaudeSessionError (decision #4).
    from setforge import claude_session as cs

    cs._resolve_claude_bin.cache_clear()
    monkeypatch.setenv("SETFORGE_CLAUDE_BIN", str(tmp_path / "nope"))
    with pytest.raises(ClaudeSessionError):
        ClaudeSession().send("x")


def test_keyboard_interrupt_kills_group_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Ctrl-C during the (detached) child must kill the group so no claude is
    # orphaned, then re-raise — the interrupt is NOT swallowed into CANCEL.
    from setforge import claude_session as cs

    killed: list[object] = []
    monkeypatch.setattr(cs, "_kill_group", lambda proc: killed.append(proc))
    monkeypatch.setattr(cs, "_resolve_claude_bin", lambda: Path("/bin/true"))

    class _FakeProc:
        pid = 4242

        def communicate(self, input: str, timeout: float) -> tuple[str, str]:
            raise KeyboardInterrupt

    monkeypatch.setattr(cs.subprocess, "Popen", lambda *a, **k: _FakeProc())
    with pytest.raises(KeyboardInterrupt):
        ClaudeSession().send("x")
    assert killed  # the process group was killed before the interrupt propagated


def test_failed_first_turn_retries_fresh(claude_stub: ClaudeStub) -> None:
    # Turn 1 fails → session stays unopened → next send retries with
    # --session-id (fresh), NOT --resume against a session that never existed.
    session = ClaudeSession()
    claude_stub.set(STUB_EXIT=1)
    with pytest.raises(ClaudeSessionError):
        session.send("first")
    claude_stub.set(STUB_EXIT=0)
    session.send("retry")
    last = claude_stub.calls()[-1]
    assert "--session-id" in last["argv"]
    assert "--resume" not in last["argv"]
