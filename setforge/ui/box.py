"""Theme-aware ``┌─┐`` box-drawing helper.

Lives apart from :mod:`setforge.ui.widgets` so a future themed diff viewer can
reuse framing without importing the interactive widget. ``frame`` is pure: it
takes already-rendered body lines plus a width and returns a list of framed
plain-text lines with balanced display widths.

Width is clamped to ``min(width, 100)`` and floored to a small minimum so a
degenerate terminal size never raises. The caller styles the returned lines
(the heading class for the title, muted for the rules) when painting them; the
prompt_toolkit class names are ``class:heading`` / ``class:muted`` (the theme
module is intentionally *not* imported here — D3 resolves the real styling).
"""

from __future__ import annotations

from collections.abc import Sequence

#: Hard upper bound on frame width (RFC: never wider than ~100 columns).
_MAX_WIDTH: int = 100
#: Floor below which the frame would have no room for content; clamps up.
_MIN_WIDTH: int = 6

_TL, _TR, _BL, _BR = "┌", "┐", "└", "┘"
_H, _V = "─", "│"


def _clamp_width(width: int) -> int:
    """Clamp a requested width into ``[_MIN_WIDTH, _MAX_WIDTH]``.

    Guards against a 0/None-shaped degenerate terminal size (the caller may
    pass ``output.get_size().columns`` which can be ``0``).
    """
    if width > _MAX_WIDTH:
        return _MAX_WIDTH
    if width < _MIN_WIDTH:
        return _MIN_WIDTH
    return width


def _top_rule(width: int, title: str | None) -> str:
    """Build the ``┌─ title ─…─┐`` top rule, exactly ``width`` chars wide."""
    inner = width - 2  # space between the two corner glyphs
    if not title:
        return _TL + (_H * inner) + _TR
    # ``┌─ <title> ─...─┐``: two leading dashes + spaces around the title.
    decorated = f"{_H} {title} "
    if len(decorated) > inner:
        # Truncate the title so the decorated segment fits the inner span.
        # Reserve room for the leading ``─ `` and a trailing space.
        budget = inner - 3
        if budget < 0:
            budget = 0
        decorated = f"{_H} {title[:budget]} "
        decorated = decorated[:inner]
    fill = inner - len(decorated)
    return _TL + decorated + (_H * fill) + _TR


def _bottom_rule(width: int) -> str:
    return _BL + (_H * (width - 2)) + _BR


def _body_row(text: str, width: int) -> str:
    """Wrap one body line as ``│ <padded/truncated> │`` at ``width``."""
    inner = width - 4  # two bars + two padding spaces
    if inner < 0:
        inner = 0
    if len(text) > inner:
        text = text[:inner]
    padded = text.ljust(inner)
    return f"{_V} {padded} {_V}"


def frame(
    lines: Sequence[str],
    *,
    title: str | None = None,
    width: int,
) -> list[str]:
    """Frame ``lines`` in a ``┌─┐`` box of clamped ``width``.

    Returns the top rule, one ``│ … │`` row per input line, and the bottom
    rule — every returned line has the same display width. ``title`` (if
    given) is drawn on the top rule and truncated to fit. ``width`` is clamped
    to ``[_MIN_WIDTH, _MAX_WIDTH]``.
    """
    w = _clamp_width(width)
    out = [_top_rule(w, title)]
    out.extend(_body_row(line, w) for line in lines)
    out.append(_bottom_rule(w))
    return out
