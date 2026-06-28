"""Unit tests for the A5c per-hunk share auto-draft sub-flow.

Drives :func:`share_draft.draft_hunk` with the ClaudeSession + UI prompts
monkeypatched, so the loop/decision logic is tested without a real ``claude``
binary or a TTY.
"""

from __future__ import annotations

import pytest

from setforge.claude_session import ClaudeSessionError
from setforge.reconcile import share_draft
from setforge.reconcile.share_draft import _Choice
from setforge.ui.widgets import CANCEL

_REGION = b"workdir: /home/raul/projects\n"


class _FakeSession:
    """A ClaudeSession stand-in whose .send returns a canned draft."""

    draft = "workdir: $HOME/projects\n"

    def send(self, prompt: str) -> str:
        return self.draft


def _patch(monkeypatch, *, instructions, reviews, session=_FakeSession) -> None:
    monkeypatch.setattr(share_draft, "ClaudeSession", session)
    monkeypatch.setattr(share_draft, "text_prompt", lambda **_: next(instructions))
    monkeypatch.setattr(share_draft, "_review", lambda clean, *, style: next(reviews))


def test_keep_local_returns_draft_no_adopt(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, instructions=iter([""]), reviews=iter([_Choice.KEEP]))
    res = share_draft.draft_hunk(_REGION, display_path="CLAUDE.md")
    assert isinstance(res, share_draft.DraftResult)
    assert res.draft == b"workdir: $HOME/projects\n"
    assert res.adopt is False


def test_adopt_returns_draft_with_adopt(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, instructions=iter([""]), reviews=iter([_Choice.ADOPT]))
    res = share_draft.draft_hunk(_REGION, display_path="CLAUDE.md")
    assert isinstance(res, share_draft.DraftResult)
    assert res.adopt is True


def test_back_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, instructions=iter([""]), reviews=iter([_Choice.BACK]))
    assert share_draft.draft_hunk(_REGION, display_path="CLAUDE.md") is CANCEL


def test_instruction_cancel_constructs_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Esc at the instruction prompt → CANCEL without ever sending a turn.
    sent: list[str] = []

    class _Track(_FakeSession):
        def send(self, prompt: str) -> str:
            sent.append(prompt)
            return self.draft

    _patch(monkeypatch, instructions=iter([CANCEL]), reviews=iter([]), session=_Track)
    assert share_draft.draft_hunk(_REGION, display_path="CLAUDE.md") is CANCEL
    assert sent == []  # no session turn was spent


def test_reprompt_then_keep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-prompt loops back for a new instruction turn, then Keep accepts.
    _patch(
        monkeypatch,
        instructions=iter(["", ""]),
        reviews=iter([_Choice.REPROMPT, _Choice.KEEP]),
    )
    res = share_draft.draft_hunk(_REGION, display_path="CLAUDE.md")
    assert isinstance(res, share_draft.DraftResult)


def test_session_error_degrades_to_reprompt(monkeypatch: pytest.MonkeyPatch) -> None:
    # A ClaudeSessionError must NOT abort — it re-prompts; a later Esc cancels.
    class _Boom(_FakeSession):
        def send(self, prompt: str) -> str:
            raise ClaudeSessionError("claude not found")

    _patch(
        monkeypatch, instructions=iter(["", CANCEL]), reviews=iter([]), session=_Boom
    )
    assert share_draft.draft_hunk(_REGION, display_path="CLAUDE.md") is CANCEL


@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        ("workdir: $HOME\n", "workdir: $HOME\n"),
        ("```\nworkdir: $HOME\n```", "workdir: $HOME"),  # wrapping fence stripped
        ("   ", None),  # empty → re-prompt
        ("has\x00nul", None),  # control char → rejected (untrusted-output gate)
    ],
)
def test_validate_gate(draft: str, expected: str | None) -> None:
    assert share_draft._validate(draft) == expected
