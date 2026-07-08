"""Acceptance + unit tests for :mod:`setforge.reconcile.claude_merge` (A4).

The full resumable sub-flow is driven end-to-end through
:func:`resolve_conflicts` with the real :func:`make_claude_merge_fn` injected.
Both interactive widgets (the instruction ``text_prompt`` and the draft-review
``button_bar``) read from one piped byte string — the proven headless pattern
from ``tests/reconcile/test_wizard.py``. ``claude`` itself is faked at the
subprocess boundary by the shared ``claude_stub`` fixture.

Keystroke legend (region menu → instruction → review):
``c`` enter Claude-merge · text + ``\\r`` submit instruction (``\\r`` alone =
empty → default prompt) · ``a`` Accept · ``r`` Re-prompt · ``e`` Edit ·
``b`` ← Back · ``\\x1b`` Esc.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from setforge.reconcile import WizardResult, resolve_conflicts
from setforge.reconcile.claude_merge import (
    _build_prompt,
    _strip_fence,
    _validate,
    make_claude_merge_fn,
)
from setforge.reconcile.merge_model import Conflict, MergeResult
from setforge.reconcile.types import FileId
from setforge.ui.widgets import CANCEL
from tests.conftest import ClaudeStub

_FID = FileId("claude/CLAUDE.md")
_DISPLAY = "~/.claude/CLAUDE.md"


def _run(keys: bytes, *, conflict: Conflict | None = None) -> object:
    """Drive ``resolve_conflicts`` over one conflict with the real Claude-merge fn."""
    region = conflict or Conflict(base=b"base\n", ours=b"A\n", theirs=b"B\n")
    result = MergeResult((region,))
    fn = make_claude_merge_fn(display_path=_DISPLAY)
    with create_pipe_input() as pipe:
        pipe.send_bytes(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            return resolve_conflicts(_FID, result, claude_merge=fn)


# --------------------------------------------------------------------------- #
# Acceptance — the carved contract
# --------------------------------------------------------------------------- #


def test_drafts_a_merge_and_accepts(claude_stub: ClaudeStub) -> None:
    claude_stub.set(STUB_STDOUT="MERGED\n")
    out = _run(b"cguide\ra")  # Claude-merge, instruction "guide", Accept
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"MERGED\n"
    (call,) = claude_stub.calls()
    stdin = call["stdin"]
    # Turn-1 prompt carries all three fenced sides + the typed instruction.
    for marker in (
        "--BASE--",
        "base",
        "--OURS--",
        "--THEIRS--",
        "YOUR INSTRUCTION",
        "guide",
    ):
        assert marker in stdin


def test_empty_instruction_uses_default_prompt(claude_stub: ClaudeStub) -> None:
    claude_stub.set(STUB_STDOUT="MERGED\n")
    out = _run(b"c\ra")  # Claude-merge, empty instruction (Enter), Accept
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"MERGED\n"
    (call,) = claude_stub.calls()
    stdin = call["stdin"]
    # The default prompt is the 3-way rules header with NO user-instruction block.
    assert "--BASE--" in stdin
    assert "YOUR INSTRUCTION" not in stdin


def test_reprompt_resumes_same_session(claude_stub: ClaudeStub) -> None:
    claude_stub.set(STUB_STDOUT="DRAFT\n")
    # draft → Re-prompt ("again") → Accept.
    out = _run(b"cfirst\rragain\ra")
    assert isinstance(out, WizardResult)
    first, second = claude_stub.calls()
    sid = first["argv"][first["argv"].index("--session-id") + 1]
    assert "--resume" in second["argv"]
    assert second["argv"][second["argv"].index("--resume") + 1] == sid
    # The refine turn carries just the instruction, not the whole 3-way prompt.
    assert "--BASE--" not in second["stdin"]
    assert "again" in second["stdin"]


def test_back_returns_to_region_menu(claude_stub: ClaudeStub) -> None:
    claude_stub.set(STUB_STDOUT="DRAFT\n")
    # Claude-merge → empty instruction → draft → ← Back → region menu → Ours.
    out = _run(b"c\rbo")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"A\n"  # file NOT aborted; Ours reachable


def test_instruction_esc_returns_to_region_menu(claude_stub: ClaudeStub) -> None:
    # Esc at the instruction prompt backs out to the region menu (not file abort).
    out = _run(b"c\x1bt")  # Claude-merge → Esc → region menu → Theirs
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"B\n"
    assert claude_stub.calls() == []  # never reached claude


# --------------------------------------------------------------------------- #
# Failure degrades to a re-prompt (decision #4)
# --------------------------------------------------------------------------- #


def test_claude_failure_degrades_to_reprompt(claude_stub: ClaudeStub) -> None:
    claude_stub.set(STUB_EXIT=2, STUB_STDERR="model error\n")
    # Claude-merge → instruction → send fails → re-prompt → Esc → region → Ours.
    out = _run(b"c\r\x1bo")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"A\n"  # degraded, never crashed; Ours reachable
    assert claude_stub.calls()  # the failing turn was attempted


def test_missing_binary_degrades_to_reprompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A missing binary is a re-promptable fault too (NOT propagated).
    from setforge import claude_session as cs

    cs._resolve_claude_bin.cache_clear()
    monkeypatch.setenv("SETFORGE_CLAUDE_BIN", str(tmp_path / "nonexistent"))
    out = _run(b"c\r\x1bt")  # Claude-merge → fails → re-prompt → Esc → Theirs
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"B\n"


def test_control_char_draft_folds_raw_bytes(claude_stub: ClaudeStub) -> None:
    # A draft carrying a control char (ESC) is sanitized only for the review
    # panel; the accepted fold uses the RAW bytes, byte-exact.
    claude_stub.set(STUB_STDOUT="line1\x1bline2\n")
    out = _run(b"c\ra")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"line1\x1bline2\n"


def test_markered_draft_reprompts(claude_stub: ClaudeStub) -> None:
    # A draft that still carries conflict markers is rejected (re-prompt), so the
    # user backs out to the region menu and picks Ours.
    claude_stub.set(STUB_STDOUT="<<<<<<< x\nA\n=======\nB\n>>>>>>> y\n")
    out = _run(b"c\r\x1bo")
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"A\n"


# --------------------------------------------------------------------------- #
# Edit sub-flow (reuses $EDITOR, seeded with the draft)
# --------------------------------------------------------------------------- #


def test_edit_returns_edited_bytes(
    claude_stub: ClaudeStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_stub.set(STUB_STDOUT="DRAFT\n")
    seen: list[str] = []

    def _fake_editor(target: Path) -> None:
        seen.append(target.read_text(encoding="utf-8"))  # editor sees the draft
        target.write_text("EDITED\n", encoding="utf-8")

    monkeypatch.setattr("setforge.reconcile._claude_ui.run_editor", _fake_editor)
    out = _run(b"c\re")  # Claude-merge → empty instruction → draft → Edit
    assert isinstance(out, WizardResult)
    assert out.merged.merged() == b"EDITED\n"
    assert seen == ["DRAFT\n"]  # editor was seeded with the accepted draft


# --------------------------------------------------------------------------- #
# Pure validation / prompt-shape units (decision #7)
# --------------------------------------------------------------------------- #


def test_validate_strips_wrapping_fence() -> None:
    assert _validate("```\nmerged line\n```") == "merged line"


def test_validate_strips_language_fence() -> None:
    assert _validate("```md\n# title\n```") == "# title"


def test_validate_rejects_empty() -> None:
    assert _validate("   \n") is None


def test_validate_rejects_leftover_markers() -> None:
    assert _validate("<<<<<<< x\na\n=======\nb\n>>>>>>> y\n") is None


def test_validate_passes_plain_text() -> None:
    assert _validate("plain merged\n") == "plain merged\n"


def test_strip_fence_leaves_unfenced_text() -> None:
    assert _strip_fence("no fence here\n") == "no fence here\n"


def test_build_prompt_fences_each_side_with_one_token() -> None:
    conflict = Conflict(base=b"BB\n", ours=b"OO\n", theirs=b"TT\n")
    prompt = _build_prompt(conflict, "do it", _DISPLAY)
    assert _DISPLAY in prompt
    # The load-bearing injection-hardening invariant: ONE per-invocation token,
    # used as a fence line around each of the 4 blocks (base/ours/theirs +
    # instruction) → at least 8 fence lines. `>= 8` (not a brittle `== 9`) so a
    # header reword that also names the token can't break a behavior-preserving change.
    tokens = set(re.findall(r"^[0-9a-f]{32}$", prompt, re.MULTILINE))
    assert len(tokens) == 1
    (token,) = tokens
    assert prompt.count(token) >= 8
    for content in ("BB", "OO", "TT", "do it"):
        assert content in prompt


def test_build_prompt_omits_instruction_block_when_empty() -> None:
    conflict = Conflict(base=b"BB\n", ours=b"OO\n", theirs=b"TT\n")
    prompt = _build_prompt(conflict, "", _DISPLAY)
    assert "YOUR INSTRUCTION" not in prompt


def test_make_fn_returns_cancel_sentinel_on_back(claude_stub: ClaudeStub) -> None:
    # The factory's fn returns the EXACT CANCEL sentinel (not a falsy stand-in)
    # when the user backs out — the value the wizard tests with `is CANCEL`.
    claude_stub.set(STUB_STDOUT="DRAFT\n")
    fn = make_claude_merge_fn(display_path=_DISPLAY)
    conflict = Conflict(base=b"base\n", ours=b"A\n", theirs=b"B\n")
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\rb")  # empty instruction → draft → ← Back
        with create_app_session(input=pipe, output=DummyOutput()):
            assert fn(conflict) is CANCEL
