"""TUI line picker for ``setforge section add``'s anchor-line selection.

Built on prompt_toolkit's full-screen :class:`Application` API:

- :class:`BufferControl` over a read-only :class:`Buffer` of the
  tracked file's text.
- Custom :class:`KeyBindings`: ↑/↓ move cursor by one line; PgUp/PgDn
  page; Home/End jump to first/last line; ``/`` triggers a
  :class:`SearchToolbar`; Enter confirms the current cursor line;
  Esc / Ctrl-C cancel with a ``None`` sentinel.
- Status bar at the bottom showing filename, current line / total
  lines, and key hints.

POSIX-only (Debian VM, headless). No Windows support intended.
"""

from __future__ import annotations

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.search import start_search
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import SearchToolbar

_PAGE_LINES: int = 10


def _move_down_clamped(buffer: Buffer, *, count: int) -> None:
    """``cursor_down`` capped at the last non-empty row."""
    target = min(buffer.document.cursor_position_row + count, _last_content_row(buffer))
    delta = target - buffer.document.cursor_position_row
    if delta > 0:
        buffer.cursor_down(count=delta)


def _jump_to_last_content_row(buffer: Buffer) -> None:
    """Move the cursor to the start of the last non-empty row."""
    target_row = _last_content_row(buffer)
    delta = target_row - buffer.document.cursor_position_row
    if delta > 0:
        buffer.cursor_down(count=delta)
    elif delta < 0:
        buffer.cursor_up(count=-delta)


def _last_content_row(buffer: Buffer) -> int:
    """Return the 0-indexed row of the last non-empty line.

    A trailing ``\\n`` in the source text produces a phantom empty row
    at ``line_count - 1``. We treat the last content row as the
    anchorable bottom of the file (insertion after a phantom row would
    no-op the cursor move and is never what the user wants).
    """
    lines = buffer.document.lines
    last = len(lines) - 1
    while last > 0 and lines[last] == "":
        last -= 1
    return last


def pick_anchor_line(*, file_text: str, filename: str) -> int | None:
    """Open a TUI file viewer; return the 1-indexed line the user picked.

    Returns ``None`` if the user cancels with Esc or Ctrl-C, or if
    ``file_text`` is empty (no lines to pick).

    Tests drive this function via prompt_toolkit's
    :func:`~prompt_toolkit.application.create_app_session` context
    manager (see
    https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html#testing).
    Production callers invoke it bare and the real terminal is used.
    """
    if not file_text:
        return None
    buffer = Buffer(
        document=Document(file_text, cursor_position=0),
        read_only=True,
    )
    return _run_picker(buffer=buffer, filename=filename)


def _status_text(buffer: Buffer, filename: str) -> str:
    line = buffer.document.cursor_position_row + 1
    total = buffer.document.line_count
    return (
        f"  [{filename}]  line {line}/{total}  "
        f"  [Enter] confirm   [/] search   [PgUp/PgDn] page   "
        f"[Home/End] jump   [Esc] cancel"
    )


def _bind_motion_keys(kb: KeyBindings, buffer: Buffer) -> None:
    """Cursor-motion keys: ↑/↓ / PgUp/PgDn / Home/End."""

    @kb.add("up")
    def _(event: KeyPressEvent) -> None:
        buffer.cursor_up(count=1)

    @kb.add("down")
    def _(event: KeyPressEvent) -> None:
        _move_down_clamped(buffer, count=1)

    @kb.add("pageup")
    def _(event: KeyPressEvent) -> None:
        buffer.cursor_up(count=_PAGE_LINES)

    @kb.add("pagedown")
    def _(event: KeyPressEvent) -> None:
        _move_down_clamped(buffer, count=_PAGE_LINES)

    @kb.add("home")
    def _(event: KeyPressEvent) -> None:
        buffer.cursor_position = 0

    @kb.add("end")
    def _(event: KeyPressEvent) -> None:
        _jump_to_last_content_row(buffer)


def _bind_terminal_keys(
    kb: KeyBindings,
    *,
    buffer: Buffer,
    buffer_control: BufferControl,
    result_holder: dict[str, int | None],
) -> None:
    """Keys that exit or open auxiliary UI: ``/`` / Enter / Esc / Ctrl-C."""

    @kb.add("/")
    def _(event: KeyPressEvent) -> None:
        start_search(buffer_control)

    @kb.add("enter")
    def _(event: KeyPressEvent) -> None:
        row = min(buffer.document.cursor_position_row, _last_content_row(buffer))
        result_holder["line"] = row + 1
        event.app.exit()

    @kb.add("escape", eager=True)
    def _(event: KeyPressEvent) -> None:
        result_holder["line"] = None
        event.app.exit()

    @kb.add("c-c")
    def _(event: KeyPressEvent) -> None:
        result_holder["line"] = None
        event.app.exit()


def _build_keybindings(
    *,
    buffer: Buffer,
    buffer_control: BufferControl,
    result_holder: dict[str, int | None],
) -> KeyBindings:
    kb = KeyBindings()
    _bind_motion_keys(kb, buffer)
    _bind_terminal_keys(
        kb,
        buffer=buffer,
        buffer_control=buffer_control,
        result_holder=result_holder,
    )
    return kb


def _run_picker(*, buffer: Buffer, filename: str) -> int | None:
    """Run a prompt_toolkit Application around ``buffer``; return the line."""
    result_holder: dict[str, int | None] = {"line": None}

    search_toolbar = SearchToolbar()
    buffer_control = BufferControl(
        buffer=buffer,
        search_buffer_control=search_toolbar.control,
    )
    body = Window(content=buffer_control)
    status = Window(
        height=1,
        content=FormattedTextControl(text=lambda: _status_text(buffer, filename)),
        style="reverse",
    )
    layout = Layout(HSplit([body, status, search_toolbar]))

    style = Style.from_dict({})
    kb = _build_keybindings(
        buffer=buffer,
        buffer_control=buffer_control,
        result_holder=result_holder,
    )
    app: Application[None] = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        style=style,
    )
    app.run()
    return result_holder["line"]
