"""Reusable button-bar widget — the project's shared selection primitive.

One custom prompt_toolkit full-screen :class:`Application`: an optional
read-only ``body`` panel above a row of focusable buttons, wrapped in a
``┌─┐`` frame (drawn by :mod:`setforge.ui.box`). Navigation is ←/→ + Tab/S-Tab;
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
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.containers import AnyContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style

from setforge.ui.box import frame

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

# Local palette so the widget renders in colour pre-merge. D3 swaps this for
# pt_style(THEME) once the theme module lands; the class names are stable. ANSI
# colour names (not hex) keep this UX-3-clean — concrete truecolor values are
# the theme module's job, not the widget's.
_STYLE: Style = Style.from_dict(
    {
        "accent": "ansibrightblue",
        "heading": "ansibrightmagenta",
        "muted": "ansibrightblack",
        "text": "ansiwhite",
        "identifier": "ansibrightcyan",
        "button.focused": "reverse ansibrightblue",
        "button": "ansiwhite",
    }
)


def _derive_accelerators(buttons: Sequence[Button[object]]) -> dict[int, str]:
    """Map each button index to a unique single-letter accelerator.

    Explicit ``Button.key`` values are reserved FIRST (and validated unique —
    a duplicate explicit key raises :class:`ValueError`). Each remaining button
    then claims the first free lowercase letter of its label; if every letter
    is already taken it gets no accelerator (omitted from the mapping). Keys
    that would collide with a reserved navigation key are skipped.
    """
    result: dict[int, str] = {}
    taken: set[str] = set()

    # Pass 1: reserve explicit keys, rejecting duplicates.
    for idx, btn in enumerate(buttons):
        if btn.key is None:
            continue
        key = btn.key.lower()
        if key in taken:
            raise ValueError(
                f"duplicate explicit accelerator key {btn.key!r} on button "
                f"{btn.label!r}"
            )
        taken.add(key)
        result[idx] = key

    # Pass 2: derive a first-free-letter accelerator for the rest.
    for idx, btn in enumerate(buttons):
        if idx in result:
            continue
        for ch in btn.label.lower():
            if not ch.isalpha():
                continue
            if ch in taken or ch in _RESERVED_KEYS:
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
    body: str | None,
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
        children.append(
            Window(
                content=FormattedTextControl(text=lambda: [("class:text", body)]),
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
    body: str | None = None,
    initial: int = 0,
) -> T | _Cancelled:
    """Run the button bar; return the chosen ``value`` or :data:`CANCEL`.

    ``buttons`` are laid out left-to-right (wrapping on narrow terminals).
    ``title`` is drawn on the frame's top rule, ``body`` (read-only) above the
    button row. ``initial`` is the index focused on open. Esc / Ctrl-C return
    :data:`CANCEL`. The result is :meth:`Application.run`'s exit value — the
    single result channel (no mutable result holder).
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
        style=_STYLE,
    )
    return app.run()
