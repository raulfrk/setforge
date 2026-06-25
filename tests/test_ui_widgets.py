"""Unit tests for :mod:`setforge.ui.widgets`.

The interactive ``button_bar`` is driven via
:func:`prompt_toolkit.application.create_app_session` wrapping
:func:`prompt_toolkit.input.create_pipe_input` +
:class:`prompt_toolkit.output.DummyOutput`, the same headless-driving
pattern proven in :mod:`tests.test_cli_anchor_picker`. Every test sends a
terminating key (pipe EOF does NOT auto-exit a prompt_toolkit Application).
"""

from __future__ import annotations

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from setforge.ui.widgets import (
    CANCEL,
    Button,
    _Cancelled,
    _derive_accelerators,
    button_bar,
)

# ---------------------------------------------------------------------------
# Task 1 — CANCEL sentinel, Button model, accelerator derivation (pure logic)
# ---------------------------------------------------------------------------


def test_cancel_is_singleton() -> None:
    assert CANCEL is CANCEL
    assert CANCEL is _Cancelled.TOKEN
    # Distinct from every falsy stand-in a caller might confuse it with.
    assert CANCEL is not None
    assert CANCEL != ""
    assert CANCEL != 0
    assert CANCEL is not False
    # A real, informative repr (not the bare ``object`` address).
    assert "Cancelled" in repr(CANCEL)


def test_button_frozen() -> None:
    btn = Button(label="Ours", value=1)
    with pytest.raises((AttributeError, TypeError)):
        btn.label = "Theirs"  # type: ignore[misc]
    assert btn.key is None


def test_accelerators_first_letter() -> None:
    buttons = [
        Button("Ours", 1),
        Button("Theirs", 2),
        Button("Edit", 3),
        Button("Skip", 4),
    ]
    assert _derive_accelerators(buttons) == {0: "o", 1: "t", 2: "e", 3: "s"}


def test_accelerator_collision_skips() -> None:
    # Both labels start with ``s``; the second falls through to its next
    # free letter (``k`` from "Skip").
    buttons = [Button("Save", 1), Button("Skip", 2)]
    assert _derive_accelerators(buttons) == {0: "s", 1: "k"}


def test_no_free_letter_gets_none() -> None:
    # The second label's only letter is already taken by the first.
    buttons = [Button("apple", 1), Button("a", 2)]
    acc = _derive_accelerators(buttons)
    assert acc[0] == "a"
    assert 1 not in acc  # no free letter → no accelerator


def test_explicit_key_reserved_first() -> None:
    # An explicit key is reserved before derived letters compete for it.
    buttons = [Button("Save", 1), Button("Send", 2, key="s")]
    acc = _derive_accelerators(buttons)
    assert acc[1] == "s"
    assert acc[0] == "a"  # "Save" yields to the reserved ``s``


def test_explicit_key_collision_raises() -> None:
    buttons = [Button("Save", 1, key="x"), Button("Send", 2, key="x")]
    with pytest.raises(ValueError, match="duplicate explicit accelerator"):
        _derive_accelerators(buttons)


# ---------------------------------------------------------------------------
# Task 3 — button_bar Application: nav / select / cancel
# ---------------------------------------------------------------------------

_BUTTONS: list[Button[str]] = [
    Button("Ours", "ours"),
    Button("Theirs", "theirs"),
    Button("Edit", "edit"),
    Button("Skip", "skip"),
]


def _drive(
    input_keys: bytes,
    *,
    buttons: list[Button[str]] | None = None,
    title: str | None = "Conflict 1/1",
    body: str | None = None,
    initial: int = 0,
) -> object:
    """Run :func:`button_bar` with piped input + dummy output."""
    with create_pipe_input() as pipe:
        pipe.send_bytes(input_keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            return button_bar(
                buttons if buttons is not None else _BUTTONS,
                title=title,
                body=body,
                initial=initial,
            )


def test_enter_returns_focused_value() -> None:
    # right, right → focus 3rd button, then Enter.
    assert _drive(b"\x1b[C\x1b[C\r") == "edit"


def test_enter_at_initial_returns_first() -> None:
    assert _drive(b"\r") == "ours"


def test_left_right_wrap() -> None:
    # left from index 0 wraps to the last button.
    assert _drive(b"\x1b[D\r") == "skip"


def test_tab_stab_moves() -> None:
    # tab → index 1, then Enter.
    assert _drive(b"\t\r") == "theirs"
    # shift-tab (``\x1b[Z``) from index 0 wraps backwards to last.
    assert _drive(b"\x1b[Z\r") == "skip"


def test_escape_returns_cancel() -> None:
    assert _drive(b"\x1b") is CANCEL


def test_ctrl_c_returns_cancel() -> None:
    assert _drive(b"\x03") is CANCEL


def test_accelerator_selects() -> None:
    # ``t`` is the derived accelerator for "Theirs".
    assert _drive(b"t") == "theirs"


def test_initial_focus_respected() -> None:
    assert _drive(b"\r", initial=2) == "edit"


# ---------------------------------------------------------------------------
# Task 4 — ``?`` cheat sheet + responsive layout
# ---------------------------------------------------------------------------


def test_question_toggles_cheatsheet() -> None:
    # ``?`` then Enter still resolves the focused value — the toggle does
    # not consume the subsequent selection.
    assert _drive(b"?\r") == "ours"


def test_body_panel_renders() -> None:
    out = _CaptureOutput()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=out):
            button_bar(_BUTTONS, title="T", body="a read-only body line")
    assert "a read-only body line" in out.captured()


def test_width_floor_guard_zero_size() -> None:
    # A 0-column DummyOutput must not crash the render path.
    assert _drive_with_size(b"\r", columns=0) == "ours"


def test_width_floor_guard_narrow() -> None:
    # Below the stacked-list threshold the bar still resolves a value.
    assert _drive_with_size(b"\r", columns=10) == "ours"


def test_renders_at_narrow_width() -> None:
    # A narrow but non-degenerate width renders and resolves.
    assert _drive_with_size(b"\x1b[C\r", columns=30) == "theirs"


class _CaptureOutput(DummyOutput):
    """Capturing :class:`DummyOutput` for render assertions."""

    def __init__(self, columns: int | None = 80) -> None:
        super().__init__()
        self.buffer: list[str] = []
        self._columns = columns

    def write(self, data: str) -> None:
        self.buffer.append(data)

    def write_raw(self, data: str) -> None:
        self.buffer.append(data)

    def captured(self) -> str:
        return "".join(self.buffer)

    def get_size(self) -> Size:
        rows = 24
        cols = self._columns if self._columns is not None else 0
        return Size(rows=rows, columns=cols)


def _drive_with_size(input_keys: bytes, *, columns: int) -> object:
    """Drive ``button_bar`` with a size-stubbed output to exercise responsive
    width handling (0/None floor, narrow stacking)."""
    out = _CaptureOutput(columns=columns)
    with create_pipe_input() as pipe:
        pipe.send_bytes(input_keys)
        with create_app_session(input=pipe, output=out):
            return button_bar(_BUTTONS, title="Conflict 1/1", body=None)
