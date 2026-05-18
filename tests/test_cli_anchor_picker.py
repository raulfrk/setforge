"""Unit tests for :mod:`setforge.cli._anchor_picker`.

Drive the picker via :func:`prompt_toolkit.application.create_app_session`
wrapping :func:`prompt_toolkit.input.create_pipe_input` +
:class:`prompt_toolkit.output.DummyOutput` so the tests don't need a
real terminal. See
https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html#testing
for the canonical pattern.
"""

from __future__ import annotations

from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from setforge.cli._anchor_picker import pick_anchor_line


class _CaptureOutput(DummyOutput):
    """Capturing :class:`DummyOutput` for status-bar rendering assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.buffer: list[str] = []

    def write(self, data: str) -> None:
        self.buffer.append(data)

    def write_raw(self, data: str) -> None:
        self.buffer.append(data)

    def captured(self) -> str:
        return "".join(self.buffer)

_FIXTURE: str = "line one\nline two\nline three\nline four\nline five\n"


def _drive(input_keys: bytes, *, file_text: str = _FIXTURE) -> int | None:
    """Run :func:`pick_anchor_line` with piped input + dummy output."""
    with create_pipe_input() as pipe:
        pipe.send_bytes(input_keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            return pick_anchor_line(file_text=file_text, filename="test.md")


def test_picker_returns_line_1_when_enter_at_top() -> None:
    assert _drive(b"\r") == 1


def test_picker_arrow_down_then_enter_returns_line_2() -> None:
    assert _drive(b"\x1b[B\r") == 2


def test_picker_two_arrow_downs_then_enter_returns_line_3() -> None:
    assert _drive(b"\x1b[B\x1b[B\r") == 3


def test_picker_arrow_up_clamps_at_top() -> None:
    assert _drive(b"\x1b[A\r") == 1


def test_picker_end_jumps_to_last_line() -> None:
    assert _drive(b"\x1b[F\r") == 5


def test_picker_home_jumps_to_first_line() -> None:
    assert _drive(b"\x1b[F\x1b[H\r") == 1


def test_picker_pgdn_advances_by_page_clamped_to_last() -> None:
    # Page size is 10 lines; on a 5-line fixture, PgDn jumps to last line.
    assert _drive(b"\x1b[6~\r") == 5


def test_picker_pgup_retreats_by_page_clamped_to_first() -> None:
    # End to last, then PgUp; on a 5-line fixture, lands at first line.
    assert _drive(b"\x1b[F\x1b[5~\r") == 1


def test_picker_ctrl_c_returns_none() -> None:
    assert _drive(b"\x03") is None


def test_picker_empty_file_returns_none() -> None:
    assert _drive(b"\r", file_text="") is None


def test_picker_single_line_file_returns_1() -> None:
    assert _drive(b"\r", file_text="only line\n") == 1


def test_picker_handles_long_file_no_hang() -> None:
    big = "\n".join(f"row {i}" for i in range(5000)) + "\n"
    assert _drive(b"\r", file_text=big) == 1


def test_picker_slash_starts_search() -> None:
    """The ``/`` key invokes :func:`start_search` (opens the SearchToolbar).

    Asserts the keybinding is wired; the search-mode key dispatcher
    does not advance the cursor under piped input in prompt_toolkit
    (the buffer cursor only moves under a real terminal's async loop),
    so we verify the boundary call instead of the downstream cursor jump.
    """
    with (
        patch("setforge.cli._anchor_picker.start_search") as mock_start_search,
        create_pipe_input() as pipe,
    ):
        pipe.send_bytes(b"/\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            pick_anchor_line(file_text=_FIXTURE, filename="test.md")
    assert mock_start_search.call_count == 1


def test_picker_esc_cancels_returns_none() -> None:
    """The bare Esc keybinding cancels the picker, returning None.

    Distinct from the Ctrl-C path covered by
    :func:`test_picker_ctrl_c_returns_none`.
    """
    assert _drive(b"\x1b") is None


def test_picker_arrow_up_retreats_cursor_from_middle() -> None:
    """Down twice (to line 3), then up, then Enter resolves to line 2.

    Proves the up keybinding actually retreats the cursor — the earlier
    :func:`test_picker_arrow_up_clamps_at_top` only asserts clamping
    behavior at the top boundary and would still pass if Up were a no-op.
    """
    assert _drive(b"\x1b[B\x1b[B\x1b[A\r") == 2


def test_picker_status_bar_shows_filename_and_position() -> None:
    """The status bar renders the filename and the line/total counter."""
    out = _CaptureOutput()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=out):
            pick_anchor_line(file_text=_FIXTURE, filename="special-name.md")
    rendered = out.captured()
    assert "special-name.md" in rendered
    # _FIXTURE has 5 content lines + a trailing-newline phantom row, so
    # buffer.document.line_count is 6 and the status bar reads "line 1/6".
    assert "line 1/" in rendered
