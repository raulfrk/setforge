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
from collections.abc import Callable
from typing import Final

import pyte
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth, DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.styles import Style, merge_styles

from setforge.ui.widgets import (
    _MAX_WIDTH,
    _STYLE,
    CANCEL,
    Button,
    _Cancelled,
    _derive_accelerators,
    button_bar,
    pager,
    text_prompt,
)

_PagerFragments = list[tuple[str, str]]

# ---------------------------------------------------------------------------
# Task 1 — CANCEL sentinel, Button model, accelerator derivation (pure logic)
# ---------------------------------------------------------------------------


def test_legend_window_wraps() -> None:
    # The button-bar legend row must wrap (not clip mid-word) at narrow
    # widths, matching its sibling body/button Windows. Construction guard:
    # locate the legend Window in the built layout and assert wrap_lines.
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.layout import walk
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    from setforge.ui.widgets import _BarState, _build_layout

    buttons = [Button("proceed", 1), Button("abort", 2)]
    accels = _derive_accelerators(buttons)
    layout = _build_layout(buttons, accels, _BarState(focus=0), title=None, body=None)

    legend = None
    for container in walk(layout.container):
        if not isinstance(container, Window):
            continue
        control = container.content
        if not isinstance(control, FormattedTextControl):
            continue
        frags = control.text() if callable(control.text) else control.text
        rendered = "".join(seg[1] for seg in to_formatted_text(frags))
        if "Enter choose" in rendered:
            legend = container
            break
    assert legend is not None, "legend Window not found in layout"
    assert legend.wrap_lines(), "legend Window must have wrap_lines enabled"


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


def test_ui_facade_lazily_reexports_button_bar() -> None:
    import setforge.ui as ui

    assert ui.button_bar is button_bar
    with pytest.raises(AttributeError, match="has no attribute"):
        getattr(ui, "missing_widget")  # noqa: B009 - exercises PEP 562 fallback


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


def test_reserved_key_error_lists_actual_reserved_set() -> None:
    # The rejection message must name only keys that are truly reserved: a
    # single-letter accelerator can never be an arrow (arrows are bound
    # separately, not in _RESERVED_KEYS), so "arrows" must not appear.
    from setforge.ui.widgets import _RESERVED_KEYS

    buttons = [Button("Foo", 1, key="?")]
    with pytest.raises(ValueError, match="reserved") as excinfo:
        _derive_accelerators(buttons)
    message = str(excinfo.value)
    assert "arrows" not in message
    for reserved in _RESERVED_KEYS:
        assert reserved in message


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

    def __init__(self, columns: int | None = 80, rows: int = 24) -> None:
        super().__init__()
        self.buffer: list[str] = []
        self._columns = columns
        self._rows = rows

    def write(self, data: str) -> None:
        self.buffer.append(data)

    def write_raw(self, data: str) -> None:
        self.buffer.append(data)

    def captured(self) -> str:
        return "".join(self.buffer)

    def get_size(self) -> Size:
        cols = self._columns if self._columns is not None else 0
        return Size(rows=self._rows, columns=cols)


class _ShrinkingOutput(_CaptureOutput):
    """Reports 30 rows once, then 6 — simulates a mid-session SIGWINCH shrink."""

    def __init__(self) -> None:
        super().__init__(columns=80, rows=30)
        self._calls = 0

    def get_size(self) -> Size:
        self._calls += 1
        rows = 30 if self._calls <= 1 else 6
        return Size(rows=rows, columns=80)


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


def test_text_prompt_default_returned_on_blank_enter() -> None:
    assert _drive_prompt(b"\r", default="owner/name") == "owner/name"


def test_text_prompt_default_cursor_at_end_backspace_edits() -> None:
    # Cursor at end-of-text: FIRST backspace deletes the default's last char.
    assert _drive_prompt(b"\x7f\r", default="abcd") == "abc"


def test_text_prompt_default_is_editable() -> None:
    assert _drive_prompt(b"X\r", default="ab") == "abX"


def test_text_prompt_escape_over_default_still_cancels() -> None:
    assert _drive_prompt(b"\x1b", default="ignored") is CANCEL


# Real construction under a truecolor renderer catches what mono DummyOutput can't.


def _truecolor_output(buf: io.StringIO) -> Vt100_Output:
    return Vt100_Output(
        buf,
        lambda: Size(rows=24, columns=80),
        default_color_depth=ColorDepth.DEPTH_24_BIT,
    )


def test_button_bar_renders_under_truecolor() -> None:
    buf = io.StringIO()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=_truecolor_output(buf)):
            result = button_bar(_BUTTONS, title="T", body="body")
    assert result == "ours"
    assert "125;207;255" in buf.getvalue()


def test_text_prompt_renders_under_truecolor() -> None:
    buf = io.StringIO()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=_truecolor_output(buf)):
            result = text_prompt(title="T", body="body", default="d")
    assert result == "d"
    assert "125;207;255" in buf.getvalue()


_LONG_LINES: _PagerFragments = [("class:muted", f"line {i}\n") for i in range(60)]


def _drive_pager(
    input_keys: bytes,
    *,
    lines: _PagerFragments | None = None,
    out: DummyOutput | None = None,
    rows: int = 10,
) -> object:
    output = out if out is not None else _CaptureOutput(rows=rows)
    with create_pipe_input() as pipe:
        pipe.send_bytes(input_keys)
        with create_app_session(input=pipe, output=output):
            return pager(lines if lines is not None else _LONG_LINES, title="diff — f")


def test_pager_q_returns_none() -> None:
    assert _drive_pager(b"q") is None


def test_pager_enter_returns_none() -> None:
    assert _drive_pager(b"\r") is None


def test_pager_escape_returns_cancel() -> None:
    assert _drive_pager(b"\x1b\x1b") is CANCEL


def test_pager_ctrl_c_returns_cancel() -> None:
    assert _drive_pager(b"\x03") is CANCEL


def test_pager_renders_first_page() -> None:
    out = _CaptureOutput(rows=10)
    _drive_pager(b"q", out=out)
    rendered = out.captured()
    assert "line 0" in rendered
    assert "line 59" not in rendered


def test_pager_scrolls_down_reveals_later_rows() -> None:
    out = _CaptureOutput(rows=10)
    _drive_pager(b"Gq", out=out)
    assert "line 59" in out.captured()


def test_pager_shrink_clamps_offset_no_crash() -> None:
    out = _ShrinkingOutput()
    assert _drive_pager(b"Gq", out=out) is None
    assert "line 59" in out.captured()


def test_pager_empty_lines_renders() -> None:
    assert _drive_pager(b"q", lines=[]) is None


def test_pager_multiline_fragment_scrolls_to_bottom() -> None:
    # A single fragment carrying many embedded newlines: fragment count (1) !=
    # visual-line count (20). ``G`` must reveal the last visual line — a
    # fragment-index slice against a visual-line clamp would over-shoot the
    # fragment list and render nothing.
    lines: _PagerFragments = [
        ("class:muted", "".join(f"vline {i}\n" for i in range(20)))
    ]
    out = _CaptureOutput(rows=10)
    _drive_pager(b"Gq", lines=lines, out=out)
    assert "vline 19" in out.captured()


def test_pager_multiline_fragment_first_page() -> None:
    # First page of a single multi-line fragment shows the top visual lines and
    # stops within the visible window (rows=10 -> 7 visible), not the bottom.
    lines: _PagerFragments = [
        ("class:muted", "".join(f"vline {i}\n" for i in range(20)))
    ]
    out = _CaptureOutput(rows=10)
    _drive_pager(b"q", lines=lines, out=out)
    rendered = out.captured()
    assert "vline 0" in rendered
    assert "vline 19" not in rendered


def test_seed_prompt_keep_live_real_widget() -> None:
    from setforge.cli._install_helpers import _seed_prompt_interactive
    from setforge.reconcile_apply import SeedChoice

    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            choice = _seed_prompt_interactive("~/f", b"a\nb\n", b"a\nc\n")
    assert choice is SeedChoice.KEEP_LIVE


def test_seed_prompt_view_diff_then_back_then_choose() -> None:
    # Real widget construction — a scripted callback missed a prior style crash.
    from setforge.cli._install_helpers import _seed_prompt_interactive
    from setforge.reconcile_apply import SeedChoice

    with create_pipe_input() as pipe:
        pipe.send_bytes(b"vq\x1b[C\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            choice = _seed_prompt_interactive("~/f", b"a\nb\n", b"a\nc\n")
    assert choice is SeedChoice.TAKE_UPSTREAM


def test_seed_prompt_view_diff_escape_aborts() -> None:
    from setforge.cli._install_helpers import _seed_prompt_interactive

    with create_pipe_input() as pipe:
        pipe.send_bytes(b"v\x1b\x1b")
        with create_app_session(input=pipe, output=DummyOutput()):
            choice = _seed_prompt_interactive("~/f", b"a\nb\n", b"a\nc\n")
    assert choice is CANCEL


def test_seed_prompt_summary_in_body() -> None:
    out = _CaptureOutput()
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=out):
            from setforge.cli._install_helpers import _seed_prompt_interactive

            _seed_prompt_interactive("~/f", b"a\nb\n", b"a\nc\n")
    rendered = out.captured()
    assert "live vs upstream" in rendered


_WIDE_COLS: Final[int] = 140


def _wide_row_widths(drive: Callable[[Vt100_Output], None]) -> list[int]:
    buf = io.StringIO()
    out = Vt100_Output(
        buf,
        lambda: Size(rows=30, columns=_WIDE_COLS),
        default_color_depth=ColorDepth.DEPTH_24_BIT,
    )
    drive(out)
    screen = pyte.Screen(_WIDE_COLS, 30)
    pyte.Stream(screen).feed(buf.getvalue())
    return [len(row.rstrip()) for row in screen.display if row.strip()]


def test_button_bar_rows_stay_within_frame_on_wide_terminal() -> None:
    long_body = "word " * 40

    def drive(out: Vt100_Output) -> None:
        with create_pipe_input() as pipe:
            pipe.send_bytes(b"\r")
            with create_app_session(input=pipe, output=out):
                button_bar(_BUTTONS, title="Conflict 1/1", body=long_body)

    widths = _wide_row_widths(drive)
    assert widths
    assert max(widths) <= _MAX_WIDTH
    assert max(widths) < _WIDE_COLS


def test_pager_rows_stay_within_frame_on_wide_terminal() -> None:
    long_lines: _PagerFragments = [
        ("class:muted", "word " * 40 + "\n") for _ in range(5)
    ]

    def drive(out: Vt100_Output) -> None:
        with create_pipe_input() as pipe:
            pipe.send_bytes(b"q")
            with create_app_session(input=pipe, output=out):
                pager(long_lines, title="diff — f")

    widths = _wide_row_widths(drive)
    assert widths
    assert max(widths) <= _MAX_WIDTH
    assert max(widths) < _WIDE_COLS


def test_text_prompt_rows_stay_within_frame_on_wide_terminal() -> None:
    def drive(out: Vt100_Output) -> None:
        with create_pipe_input() as pipe:
            pipe.send_bytes(b"\r")
            with create_app_session(input=pipe, output=out):
                text_prompt(title="claude-merge", body="word " * 40, default="d")

    widths = _wide_row_widths(drive)
    assert widths
    assert max(widths) <= _MAX_WIDTH
    assert max(widths) < _WIDE_COLS


def test_text_prompt_buffer_left_unclamped_cursor_edits_survive() -> None:
    out = Vt100_Output(
        io.StringIO(),
        lambda: Size(rows=30, columns=_WIDE_COLS),
        default_color_depth=ColorDepth.DEPTH_24_BIT,
    )
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\x7f\r")
        with create_app_session(input=pipe, output=out):
            result = text_prompt(title="T", default="abcd")
    assert result == "abc"


def test_cheatsheet_window_wraps() -> None:
    # The cheat-sheet row (accelerator legend behind '?') must wrap like every
    # sibling Window, not clip accelerators off-screen at narrow width.
    from prompt_toolkit.layout import walk
    from prompt_toolkit.layout.containers import ConditionalContainer, Window

    from setforge.ui.widgets import _BarState, _build_layout

    buttons = [Button("proceed", 1), Button("abort", 2)]
    accels = _derive_accelerators(buttons)
    layout = _build_layout(buttons, accels, _BarState(focus=0), title=None, body=None)
    windows = [
        c.content
        for c in walk(layout.container)
        if isinstance(c, ConditionalContainer) and isinstance(c.content, Window)
    ]
    assert windows, "cheat-sheet ConditionalContainer not found"
    for win in windows:
        assert isinstance(win, Window)
        assert win.wrap_lines(), "cheat-sheet Window must wrap"
