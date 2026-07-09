"""Unit tests for :mod:`setforge.ui.widgets`.

The interactive ``button_bar`` is driven via
:func:`prompt_toolkit.application.create_app_session` wrapping
:func:`prompt_toolkit.input.create_pipe_input` +
:class:`prompt_toolkit.output.DummyOutput`, the same headless-driving
pattern proven in :mod:`tests.test_cli_anchor_picker`. Every test sends a
terminating key (pipe EOF does NOT auto-exit a prompt_toolkit Application).
"""

from __future__ import annotations

import io

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth, DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.styles import Style, merge_styles

from setforge.ui.widgets import (
    _STYLE,
    CANCEL,
    Button,
    _Cancelled,
    _derive_accelerators,
    button_bar,
    text_prompt,
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


def test_explicit_reserved_key_raises() -> None:
    # An explicit key that collides with a reserved navigation key (here ``?``,
    # the cheat-sheet toggle) is rejected rather than shadowing the binding.
    buttons = [Button("Foo", 1, key="?")]
    with pytest.raises(ValueError, match="reserved"):
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


def test_cheatsheet_footer_renders() -> None:
    # ``?`` turns the cheat sheet on; the rendered frame must actually carry the
    # letter→label legend (the "keys:" prefix plus each accelerator + label).
    out = _CaptureOutput()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"?\r")
        with create_app_session(input=pipe, output=out):
            button_bar(_BUTTONS, title="Conflict 1/1", body=None)
    rendered = out.captured()
    assert "keys:" in rendered
    # Each derived accelerator letter and its label is present in the legend.
    for letter, label in (("o", "Ours"), ("t", "Theirs"), ("e", "Edit"), ("s", "Skip")):
        assert letter in rendered
        assert label in rendered


def test_body_panel_renders() -> None:
    out = _CaptureOutput()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=out):
            button_bar(_BUTTONS, title="T", body="a read-only body line")
    assert "a read-only body line" in out.captured()


def test_body_panel_renders_fragments() -> None:
    # The fragments-body path (the coloured panel the conflict wizard uses)
    # renders its text the same as a plain str body.
    out = _CaptureOutput()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=out):
            button_bar(
                _BUTTONS,
                title="T",
                body=[
                    ("class:success", "ours side\n"),
                    ("class:warning", "theirs side"),
                ],
            )
    rendered = out.captured()
    assert "ours side" in rendered
    assert "theirs side" in rendered


def test_style_param_merges_over_widget_palette() -> None:
    # A caller style is MERGED over the widget palette, not replacing it: the
    # caller's role colour applies AND the widget's own button classes (defined
    # only by _STYLE) survive the merge.
    caller = Style.from_dict({"success": "#9ece6a"})
    merged = merge_styles([_STYLE, caller])
    assert merged.get_attrs_for_style_str("class:success").color == "9ece6a"
    # button.focused exists only in _STYLE ("reverse ...") — a replace would drop it.
    assert merged.get_attrs_for_style_str("class:button.focused").reverse


def test_style_param_plumbs_through_button_bar() -> None:
    # Passing style= end-to-end does not break selection resolution.
    style = Style.from_dict({"success": "#9ece6a"})
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            result = button_bar(_BUTTONS, title="T", body=None, style=style)
    assert result == "ours"


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


# ---------------------------------------------------------------------------
# text_prompt — single-line themed input (A4)
# ---------------------------------------------------------------------------


def _drive_prompt(
    input_keys: bytes, *, body: str | None = None, default: str = ""
) -> object:
    """Run :func:`text_prompt` with piped input + dummy output."""
    with create_pipe_input() as pipe:
        pipe.send_bytes(input_keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            return text_prompt(title="claude-merge", body=body, default=default)


def test_text_prompt_enter_submits_typed_text() -> None:
    assert _drive_prompt(b"keep ours\r") == "keep ours"


def test_text_prompt_empty_enter_returns_empty_string() -> None:
    # A blank submit is meaningful (→ use the default prompt), NOT a cancel.
    out = _drive_prompt(b"\r")
    assert out == ""
    assert out is not CANCEL


def test_text_prompt_escape_returns_cancel() -> None:
    assert _drive_prompt(b"\x1b") is CANCEL


def test_text_prompt_ctrl_c_returns_cancel() -> None:
    assert _drive_prompt(b"\x03") is CANCEL


def test_text_prompt_backspace_edits_buffer() -> None:
    # Type "abcX", delete the X (\x7f), submit → real line editing via Buffer.
    assert _drive_prompt(b"abcX\x7f\r") == "abc"


def test_text_prompt_body_renders() -> None:
    out = _CaptureOutput()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=out):
            text_prompt(title="T", body="your instruction ⟩")
    assert "your instruction ⟩" in out.captured()


# --- default= prefill -------------------------------------------------------


def test_text_prompt_default_returned_on_blank_enter() -> None:
    # A bare Enter with a default returns the default (not "").
    assert _drive_prompt(b"\r", default="owner/name") == "owner/name"


def test_text_prompt_default_cursor_at_end_backspace_edits() -> None:
    # The default is seeded via the Buffer/Document constructor with the cursor
    # at end-of-text — so the FIRST backspace deletes the default's last char
    # (a post-construction ``buffer.text = default`` would leave the cursor at 0
    # and swallow it, returning the full default unchanged).
    assert _drive_prompt(b"\x7f\r", default="abcd") == "abc"


def test_text_prompt_default_is_editable() -> None:
    # Typing after the seeded default appends at the cursor.
    assert _drive_prompt(b"X\r", default="ab") == "abX"


def test_text_prompt_escape_over_default_still_cancels() -> None:
    # Esc returns CANCEL even when a default was seeded — never the default.
    assert _drive_prompt(b"\x1b", default="ignored") is CANCEL


# --- real-construction truecolor render (headless build-crash guard) --------


def _truecolor_output(buf: io.StringIO) -> Vt100_Output:
    """A real :class:`Vt100_Output` over ``buf`` forced to 24-bit depth — so the
    themed DEFAULT style resolves to real ``#rrggbb`` SGR escapes (the render
    path a mono :class:`DummyOutput` never exercises)."""
    return Vt100_Output(
        buf,
        lambda: Size(rows=24, columns=80),
        default_color_depth=ColorDepth.DEPTH_24_BIT,
    )


def test_button_bar_renders_under_truecolor() -> None:
    # Real app.run() with the themed DEFAULT style against a truecolor Vt100
    # renderer: guards the headless build-crash that callback-scripted tests
    # miss. The theme's accent hex must reach the byte stream, and selection
    # still resolves.
    buf = io.StringIO()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=_truecolor_output(buf)):
            result = button_bar(_BUTTONS, title="T", body="body")
    assert result == "ours"
    # The "← → move · Enter choose · ? help" legend renders in class:identifier
    # (#7dcfff) → SGR "38;2;125;207;255" under 24-bit — proof the themed hex
    # reached the byte stream without a headless render crash.
    assert "125;207;255" in buf.getvalue()


def test_text_prompt_renders_under_truecolor() -> None:
    buf = io.StringIO()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=_truecolor_output(buf)):
            result = text_prompt(title="T", body="body", default="d")
    assert result == "d"
    # identifier (#7dcfff) → SGR "38;2;125;207;255" drives the hint line.
    assert "125;207;255" in buf.getvalue()
