"""Unit tests for :mod:`setforge.cli._anchor_picker`.

Drive the picker via :func:`prompt_toolkit.input.create_pipe_input` +
:class:`prompt_toolkit.output.DummyOutput` so the tests don't need a
real terminal.
"""

from __future__ import annotations

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from setforge.cli._anchor_picker import pick_anchor_line

_FIXTURE: str = "line one\nline two\nline three\nline four\nline five\n"


def _drive(input_keys: bytes, *, file_text: str = _FIXTURE) -> int | None:
    """Run :func:`pick_anchor_line` with piped input + dummy output."""
    with create_pipe_input() as inp:
        inp.send_bytes(input_keys)
        return pick_anchor_line(
            file_text=file_text,
            filename="test.md",
            _input=inp,
            _output=DummyOutput(),
        )


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
