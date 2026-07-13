"""Reusable button-bar widget — the project's shared selection primitive.

One custom prompt_toolkit full-screen :class:`Application`: an optional
read-only ``body`` panel above a row of focusable buttons, bracketed by a top
rule and bottom rule drawn by :mod:`setforge.ui.box`. Side bars are
intentionally omitted because the rows are live prompt_toolkit
:class:`~prompt_toolkit.layout.Window`\\ s. Navigation is ←/→ + Tab/S-Tab;
Enter selects the focused button; Esc / Ctrl-C cancel with the :data:`CANCEL`
sentinel. Each button carries a hidden first-letter accelerator; ``?`` toggles
a cheat-sheet footer listing them.

The single result channel is :meth:`Application.run`'s ``exit(result=…)`` value
— there is no mutable holder. ``button_bar`` returns either the chosen button's
``.value`` or :data:`CANCEL`.

Driven headlessly in tests via :func:`prompt_toolkit.application.create_app_session`
(see :mod:`tests.test_ui_widgets`). POSIX-only (headless Debian VM).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.containers import AnyContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import BaseStyle, Style, merge_styles

from setforge.ui.box import frame
from setforge.ui.theme import pt_style

#: Styled-fragment list, the shape prompt_toolkit's ``FormattedTextControl``
#: accepts: ``(style_class, text)`` pairs.
type _Fragments = list[tuple[str, str]]


class _Cancelled(Enum):
    """The type of the :data:`CANCEL` singleton (one member, ``TOKEN``)."""

    TOKEN = auto()


#: Sentinel returned when the user cancels (Esc / Ctrl-C). An Enum-singleton
#: rather than ``object()``: typeable (``T | _Cancelled``), real ``repr``, a
#: true singleton, distinct from every button value and from every falsy
#: stand-in. Callers test ``if result is CANCEL``.
CANCEL: Final = _Cancelled.TOKEN

#: Public alias for the cancel sentinel's type, so callers can spell the return
#: union of widgets that may cancel (e.g. ``WizardResult | Cancelled``) without
#: reaching for the private member name.
type Cancelled = _Cancelled


@dataclass(frozen=True, slots=True)
class Button[T]:
    """One selectable button: a ``label``, the ``value`` it yields, and an
    optional explicit accelerator ``key`` (a single lowercase letter)."""

    label: str
    value: T
    key: str | None = None


# Keys reserved by navigation / control — an accelerator may never collide with
# one of these or it would shadow the built-in binding.
_RESERVED_KEYS: frozenset[str] = frozenset({"enter", "tab", "escape", "?"})

#: Hard floor below which the button row degrades to a stacked list.
_STACK_THRESHOLD: int = 24
#: Frame width never exceeds this (mirrors :data:`box._MAX_WIDTH`).
_MAX_WIDTH: int = 100

_BUTTON_STYLE: Style = Style.from_dict(
    {
        "button.focused": "reverse ansibrightblue",
        "button": "ansiwhite",
    }
)


def _theme_style() -> Style:
    # Strip pt_style()'s "class:" prefix — Style.from_dict prepends its own.
    return Style.from_dict(
        {key.removeprefix("class:"): value for key, value in pt_style().items()}
    )


_STYLE: BaseStyle = merge_styles([_theme_style(), _BUTTON_STYLE])


def _derive_accelerators(buttons: Sequence[Button[object]]) -> dict[int, str]:
    """Map each button index to a unique single-letter accelerator.

    Explicit ``Button.key`` values are reserved FIRST (and validated unique —
    a duplicate explicit key raises :class:`ValueError`; a key that collides
    with a reserved navigation key — ``?``, arrows, enter, tab, escape — also
    raises). Each remaining button then claims the first free lowercase letter
    of its label; if every letter is already taken it gets no accelerator
    (omitted from the mapping).
    """
    result: dict[int, str] = {}
    taken: set[str] = set()

    # Pass 1: reserve explicit keys, rejecting duplicates and reserved keys.
    for idx, btn in enumerate(buttons):
        if btn.key is None:
            continue
        key = btn.key.lower()
        if key in _RESERVED_KEYS:
            raise ValueError(
                f"explicit accelerator key {btn.key!r} on button {btn.label!r} "
                f"is reserved (? , arrows, enter, tab, escape)"
            )
        if key in taken:
            raise ValueError(
                f"duplicate explicit accelerator key {btn.key!r} on button "
                f"{btn.label!r}"
            )
        taken.add(key)
        result[idx] = key

    # Pass 2: derive a first-free-letter accelerator for the rest. A derived
    # letter is a single ``.isalpha()`` char, never a reserved multi-char word
    # or ``?``, so only the ``taken`` set needs checking here.
    for idx, btn in enumerate(buttons):
        if idx in result:
            continue
        for ch in btn.label.lower():
            if not ch.isalpha():
                continue
            if ch in taken:
                continue
            taken.add(ch)
            result[idx] = ch
            break
    return result


def _frame_width() -> int:
    """Current frame width: ``min(terminal columns, 100)``, floored.

    Read at render time (never constructor time) so a resize is honoured. A
    0/None-shaped :class:`DummyOutput` size is clamped up by :func:`box.frame`;
    here we only guard the negative/zero case before arithmetic.
    """
    try:
        cols = get_app().output.get_size().columns
    except Exception:  # any output-size failure → safe default width.
        cols = _MAX_WIDTH
    if not cols or cols <= 0:
        cols = _MAX_WIDTH
    return min(cols, _MAX_WIDTH)


def _button_label(btn: Button[object], *, focused: bool) -> str:
    """Render one button's label: ``«Label»`` focused, ``[ Label ]`` idle."""
    return f"«{btn.label}»" if focused else f"[ {btn.label} ]"


@dataclass(slots=True)
class _BarState:
    """Mutable render state: which button is focused + cheat-sheet visibility."""

    focus: int
    cheat: bool = False


def _row_fragments(buttons: Sequence[Button[object]], state: _BarState) -> _Fragments:
    """FormattedText for the whole button row.

    Each button is one styled span separated by two spaces; the control's own
    ``wrap_lines`` splits a too-wide row across lines. Below the stack
    threshold each button is forced onto its own line.
    """
    stacked = _frame_width() < _STACK_THRESHOLD
    frags: _Fragments = []
    for idx, btn in enumerate(buttons):
        focused = idx == state.focus
        style = "class:button.focused" if focused else "class:button"
        frags.append((style, _button_label(btn, focused=focused)))
        frags.append(("", "\n" if stacked else "  "))
    return frags


def _cheatsheet_fragments(
    buttons: Sequence[Button[object]], accelerators: dict[int, str]
) -> _Fragments:
    parts: _Fragments = [("class:muted", "keys:  ")]
    for idx, btn in enumerate(buttons):
        key = accelerators.get(idx)
        if key is None:
            continue
        parts.append(("class:accent", key))
        parts.append(("class:muted", f" {btn.label}   "))
    return parts


def _build_layout(
    buttons: Sequence[Button[object]],
    accelerators: dict[int, str],
    state: _BarState,
    *,
    title: str | None,
    body: str | _Fragments | None,
) -> Layout:
    """Assemble the framed body/button-row/legend/cheat-sheet layout."""

    def _top_rule() -> _Fragments:
        return [("class:muted", frame([], title=title, width=_frame_width())[0])]

    def _bottom_rule() -> _Fragments:
        return [("class:muted", frame([], title=None, width=_frame_width())[-1])]

    children: list[AnyContainer] = [
        Window(content=FormattedTextControl(text=_top_rule), height=1)
    ]
    if body is not None:
        # A plain str is wrapped in the default text class (back-compat); a
        # pre-built fragments list is rendered verbatim so callers can colour it.
        body_frags: _Fragments = (
            [("class:text", body)] if isinstance(body, str) else list(body)
        )
        children.append(
            Window(
                content=FormattedTextControl(text=lambda: body_frags),
                height=Dimension(min=1),
                wrap_lines=True,
            )
        )
    children.append(
        Window(
            content=FormattedTextControl(
                text=lambda: _row_fragments(buttons, state), focusable=True
            ),
            height=Dimension(min=1),
            wrap_lines=True,
        )
    )
    children.append(
        Window(
            content=FormattedTextControl(
                text=lambda: [("class:identifier", "← → move · Enter choose · ? help")]
            ),
            height=1,
        )
    )
    children.append(
        ConditionalContainer(
            content=Window(
                content=FormattedTextControl(
                    text=lambda: _cheatsheet_fragments(buttons, accelerators)
                ),
                height=1,
            ),
            filter=Condition(lambda: state.cheat),
        )
    )
    children.append(Window(content=FormattedTextControl(text=_bottom_rule), height=1))
    return Layout(HSplit(children))


def _build_keybindings(
    buttons: Sequence[Button[object]],
    accelerators: dict[int, str],
    state: _BarState,
    *,
    select: Callable[[int, KeyPressEvent], None],
) -> KeyBindings:
    kb = KeyBindings()
    n = len(buttons)

    def _move(delta: int) -> None:
        state.focus = (state.focus + delta) % n
        state.cheat = False

    @kb.add("left")
    @kb.add("s-tab")
    def _(_event: KeyPressEvent) -> None:
        _move(-1)

    @kb.add("right")
    @kb.add("tab")
    def _(_event: KeyPressEvent) -> None:
        _move(1)

    @kb.add("enter")
    def _(event: KeyPressEvent) -> None:
        select(state.focus, event)

    @kb.add("escape", eager=True)
    def _(event: KeyPressEvent) -> None:
        event.app.exit(result=CANCEL)

    @kb.add("c-c")
    def _(event: KeyPressEvent) -> None:
        event.app.exit(result=CANCEL)

    @kb.add("?")
    def _(_event: KeyPressEvent) -> None:
        state.cheat = not state.cheat

    # Accelerators (non-eager): a letter selects its button directly. The
    # default-arg capture pins each loop's ``idx``.
    for idx, key in accelerators.items():

        @kb.add(key)
        def _(event: KeyPressEvent, _idx: int = idx) -> None:
            select(_idx, event)

    return kb


def button_bar[T](
    buttons: Sequence[Button[T]],
    *,
    title: str | None = None,
    body: str | _Fragments | None = None,
    initial: int = 0,
    style: BaseStyle | None = None,
) -> T | _Cancelled:
    """Run the button bar; return the chosen ``value`` or :data:`CANCEL`.

    ``buttons`` are laid out left-to-right (wrapping on narrow terminals).
    ``title`` is drawn on the frame's top rule, ``body`` (read-only) above the
    button row — either a plain ``str`` (rendered in the default text class) or
    a prompt_toolkit fragments list (``(style_class, text)`` pairs) for a
    coloured panel. ``initial`` is the index focused on open. ``style``, when
    given, is *merged over* the widget's built-in palette
    (:data:`merge_styles`) — so a caller's theme overrides the role colours
    while the widget's own ``button`` / ``button.focused`` classes survive;
    ``None`` keeps the themed default palette. Esc / Ctrl-C return :data:`CANCEL`.
    The result is :meth:`Application.run`'s exit value — the single result
    channel (no mutable result holder).
    """
    if not buttons:
        raise ValueError("button_bar requires at least one button")

    accelerators = _derive_accelerators(buttons)
    state = _BarState(focus=max(0, min(initial, len(buttons) - 1)))

    def _select(idx: int, event: KeyPressEvent) -> None:
        event.app.exit(result=buttons[idx].value)

    layout = _build_layout(buttons, accelerators, state, title=title, body=body)
    kb = _build_keybindings(buttons, accelerators, state, select=_select)

    app: Application[T | _Cancelled] = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        style=_STYLE if style is None else merge_styles([_STYLE, style]),
    )
    return app.run()


@dataclass(slots=True)
class _PagerState:
    """Mutable pager scroll state: the index of the first visible row."""

    offset: int = 0


def _pager_visible_rows() -> int:
    """Rows the pager body can show: terminal lines minus the chrome.

    Read at render time (never constructor time) so a SIGWINCH resize is
    honoured. The chrome is the top rule + bottom rule + the one-line legend
    (:data:`_PAGER_CHROME_ROWS`); a degenerate 0/None height floors to one
    body row so scrolling maths never divides into an empty window.
    """
    try:
        rows = get_app().output.get_size().rows
    except Exception:  # any output-size failure → a safe minimal height.
        rows = _PAGER_CHROME_ROWS + 1
    return max(1, rows - _PAGER_CHROME_ROWS)


#: Top rule + legend + bottom rule surrounding the pager's scrollable body.
_PAGER_CHROME_ROWS: Final[int] = 3


def pager(
    lines: _Fragments,
    *,
    title: str | None = None,
    style: BaseStyle | None = None,
) -> None | _Cancelled:
    """Full-screen ``less``-style pager over a fragments list.

    Renders ``lines`` (``(style_class, text)`` pairs — the shape
    :func:`~setforge.ui.diffview.to_fragments` returns) inside a framed,
    scrollable window. Keys: ↑/↓ + j/k scroll one row, PageUp/PageDown +
    Space scroll a page, g/G jump to top/bottom, q / Enter return ``None`` (go
    back), Esc / Ctrl-C return :data:`CANCEL`. The two exits are distinct so a
    caller can propagate an abort — the seed prompt maps a pager :data:`CANCEL`
    onto a whole-file abort while ``q`` returns to the button bar.

    Scroll geometry is recomputed on every render from ``output.get_size()``
    (:func:`_pager_visible_rows`), and the offset is clamped to
    ``max(0, total - visible)`` on every scroll AND every render, so a terminal
    shrink can never leave the window scrolled past the last row.
    """
    if not lines:
        lines = [("class:muted", "(empty)\n")]
    total = sum(frag_text.count("\n") or 1 for _cls, frag_text in lines)
    state = _PagerState()

    def _clamp() -> None:
        state.offset = max(0, min(state.offset, total - _pager_visible_rows()))

    def _body() -> _Fragments:
        _clamp()
        # Window the fragments to the visible slice. Each fragment is a single
        # logical line (to_fragments emits one row per fragment, newline-
        # terminated), so row-index slicing over the fragment list is exact.
        visible = _pager_visible_rows()
        return list(lines[state.offset : state.offset + visible])

    def _top_rule() -> _Fragments:
        return [("class:muted", frame([], title=title, width=_frame_width())[0])]

    def _legend() -> _Fragments:
        return [
            (
                "class:identifier",
                "↑↓/jk scroll · space/PgDn page · g/G top/bottom · q back",
            )
        ]

    def _bottom_rule() -> _Fragments:
        return [("class:muted", frame([], title=None, width=_frame_width())[-1])]

    body_window = Window(
        content=FormattedTextControl(text=_body, focusable=True),
        height=Dimension(min=1),
    )
    layout = Layout(
        HSplit(
            [
                Window(content=FormattedTextControl(text=_top_rule), height=1),
                body_window,
                Window(content=FormattedTextControl(text=_legend), height=1),
                Window(content=FormattedTextControl(text=_bottom_rule), height=1),
            ]
        ),
        focused_element=body_window,
    )
    app: Application[None | _Cancelled] = Application(
        layout=layout,
        key_bindings=_pager_keybindings(state, total, _clamp),
        full_screen=True,
        style=_STYLE if style is None else merge_styles([_STYLE, style]),
    )
    return app.run()


def _pager_keybindings(
    state: _PagerState, total: int, clamp: Callable[[], None]
) -> KeyBindings:
    """Scroll + exit bindings for :func:`pager`.

    ↑/↓ + j/k step one row; PageUp/PageDown + Space step a page (the current
    visible height); g/G jump to the top / the last page; q + Enter exit with
    ``None`` (back); Esc / Ctrl-C exit with :data:`CANCEL` (abort the file).
    """
    kb = KeyBindings()

    def _scroll(delta: int) -> None:
        state.offset += delta
        clamp()

    # (keys, step) — step is a callable so a page is sized at press time from
    # the current visible height (honouring a resize between presses).
    steppers: list[tuple[tuple[str, ...], Callable[[], int]]] = [
        (("down", "j"), lambda: 1),
        (("up", "k"), lambda: -1),
        (("pagedown", "space"), _pager_visible_rows),
        (("pageup",), lambda: -_pager_visible_rows()),
    ]
    for keys, step in steppers:
        handler = _make_scroll_handler(_scroll, step)
        for key in keys:
            kb.add(key)(handler)

    @kb.add("g")
    def _(_event: KeyPressEvent) -> None:
        state.offset = 0

    @kb.add("G")
    def _(_event: KeyPressEvent) -> None:
        state.offset = total  # clamped down to the last page on next render.
        clamp()

    for key in ("q", "enter"):
        kb.add(key)(_make_exit_handler(None))
    kb.add("escape", eager=True)(_make_exit_handler(CANCEL))
    kb.add("c-c")(_make_exit_handler(CANCEL))
    return kb


def _make_scroll_handler(
    scroll: Callable[[int], None], step: Callable[[], int]
) -> Callable[[KeyPressEvent], None]:
    def _handler(_event: KeyPressEvent) -> None:
        scroll(step())

    return _handler


def _make_exit_handler(result: None | _Cancelled) -> Callable[[KeyPressEvent], None]:
    def _handler(event: KeyPressEvent) -> None:
        event.app.exit(result=result)

    return _handler


def _text_prompt_keybindings() -> KeyBindings:
    """Esc / Ctrl-C cancel; Enter is owned by the buffer's accept handler."""
    kb = KeyBindings()

    @kb.add("escape", eager=True)
    def _(event: KeyPressEvent) -> None:
        event.app.exit(result=CANCEL)

    @kb.add("c-c")
    def _(event: KeyPressEvent) -> None:
        event.app.exit(result=CANCEL)

    return kb


def _text_prompt_layout(
    buffer: Buffer, *, title: str | None, body: str | _Fragments | None
) -> Layout:
    """Framed (optional) body panel above the single-line input window.

    Mirrors :func:`_build_layout`'s top/bottom-rule framing; the input window is
    the focused element so typed keys land in ``buffer``.
    """

    def _top_rule() -> _Fragments:
        return [("class:muted", frame([], title=title, width=_frame_width())[0])]

    def _bottom_rule() -> _Fragments:
        return [("class:muted", frame([], title=None, width=_frame_width())[-1])]

    children: list[AnyContainer] = [
        Window(content=FormattedTextControl(text=_top_rule), height=1)
    ]
    if body is not None:
        body_frags: _Fragments = (
            [("class:text", body)] if isinstance(body, str) else list(body)
        )
        children.append(
            Window(
                content=FormattedTextControl(text=lambda: body_frags),
                height=Dimension(min=1),
                wrap_lines=True,
            )
        )
    input_window = Window(content=BufferControl(buffer=buffer), height=1)
    children.append(input_window)
    children.append(
        Window(
            content=FormattedTextControl(
                text=lambda: [("class:identifier", "Enter submit · Esc cancel")]
            ),
            height=1,
        )
    )
    children.append(Window(content=FormattedTextControl(text=_bottom_rule), height=1))
    return Layout(HSplit(children), focused_element=input_window)


def text_prompt(
    *,
    title: str | None = None,
    body: str | _Fragments | None = None,
    default: str = "",
    style: BaseStyle | None = None,
) -> str | _Cancelled:
    """Full-screen single-line themed text input — a sibling of :func:`button_bar`.

    Renders a framed ``body`` panel (optional, same ``str`` / fragments shape as
    ``button_bar``) above a one-line editable buffer. **Enter** submits the
    current text — which **may be empty**: a blank submit returns the current
    buffer text (``default`` if the buffer was left untouched, else ``""``) — a
    meaningful answer, e.g. "use the default", never a cancel. **Esc / Ctrl-C**
    return :data:`CANCEL`. ``style`` is merged over the widget's built-in palette
    exactly like ``button_bar``; ``None`` keeps the themed default palette.

    ``default`` is seeded via the :class:`Buffer`/:class:`Document` constructor
    with the cursor at end-of-text, so the first Backspace deletes its last
    char (a post-construction ``buffer.text =`` would leave the cursor at 0).

    Layout and keybindings are factored into :func:`_text_prompt_layout` /
    :func:`_text_prompt_keybindings`, matching ``button_bar``'s decomposition.
    Driven headlessly in tests via ``create_app_session`` + a pipe input, the
    same pattern as ``button_bar`` (text bytes then a terminating ``\\r``).
    """
    buffer = Buffer(
        document=Document(text=default, cursor_position=len(default)),
        multiline=False,
    )

    def _accept(buff: Buffer) -> bool:
        # Enter on a single-line buffer fires the accept handler; exit with the
        # text (possibly "") as the single result channel — no mutable holder.
        get_app().exit(result=buff.text)
        return True  # keep the buffer text (no validation rejection)

    buffer.accept_handler = _accept

    app: Application[str | _Cancelled] = Application(
        layout=_text_prompt_layout(buffer, title=title, body=body),
        key_bindings=_text_prompt_keybindings(),
        full_screen=True,
        style=_STYLE if style is None else merge_styles([_STYLE, style]),
    )
    return app.run()
