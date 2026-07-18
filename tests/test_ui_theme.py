"""Unit tests for the Tokyo Night theme module (setforge/ui/theme.py).

Covers, per the theme acceptance contract: every role resolves in the THEME table;
xterm256 indices are valid + chromatic roles sit in the colour cube; Color is
frozen; the per-stream capability precedence matrix (NO_COLOR set/empty/unset x
isatty T/F x COLORTERM truecolor/24bit/junk/unset); and each render adapter's
exact output for one role in truecolor / 256 / mono (sgr ends in reset, correct
introducer, no escapes to a piped stream).
"""

from __future__ import annotations

import dataclasses
import io

import pytest

from setforge.ui.theme import (
    RESET,
    THEME,
    Cap,
    Color,
    Role,
    capability,
    pt_style,
    render_demo,
    sgr,
    styled,
)

CHROMATIC = (
    Role.ACCENT,
    Role.SUCCESS,
    Role.ERROR,
    Role.WARNING,
    Role.HEADING,
    Role.IDENTIFIER,
)


# --------------------------------------------------------------------------- #
# Fake stream — controllable isatty() (True / False / raises).
# --------------------------------------------------------------------------- #
class _FakeStream:
    def __init__(self, tty: bool | type[BaseException]) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        if isinstance(self._tty, type) and issubclass(self._tty, BaseException):
            raise self._tty("closed stream")
        return bool(self._tty)


_TTY = _FakeStream(True)
_PIPE = _FakeStream(False)


# --------------------------------------------------------------------------- #
# Task 1 — Role / Color / THEME
# --------------------------------------------------------------------------- #
def test_every_role_has_a_color() -> None:
    assert set(Role) == set(THEME)
    for color in THEME.values():
        assert isinstance(color, Color)


def test_xterm256_in_range() -> None:
    for color in THEME.values():
        assert 0 <= color.xterm256 <= 255


def test_chromatic_roles_in_cube() -> None:
    for role in CHROMATIC:
        assert 16 <= THEME[role].xterm256 <= 231


def test_color_is_frozen() -> None:
    color = THEME[Role.ACCENT]
    with pytest.raises(dataclasses.FrozenInstanceError):
        color.xterm256 = 5  # type: ignore[misc]


def test_truecolor_is_hex() -> None:
    for color in THEME.values():
        assert color.truecolor.startswith("#")
        assert len(color.truecolor) == 7


# --------------------------------------------------------------------------- #
# Task 2 — capability precedence matrix (per stream)
# --------------------------------------------------------------------------- #
def test_no_color_set_nonempty_is_mono(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert capability(_TTY) is Cap.MONO


def test_no_color_empty_string_does_not_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "")  # empty → NOT disabling (no-color.org)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert capability(_TTY) is Cap.TRUECOLOR


def test_non_tty_is_mono(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert capability(_PIPE) is Cap.MONO


def test_colorterm_truecolor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert capability(_TTY) is Cap.TRUECOLOR


def test_colorterm_24bit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "24bit")
    assert capability(_TTY) is Cap.TRUECOLOR


def test_colorterm_junk_falls_to_256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "yes")  # not exact membership
    assert capability(_TTY) is Cap.C256


def test_colorterm_unset_falls_to_256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    assert capability(_TTY) is Cap.C256


def test_closed_stream_treated_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert capability(_FakeStream(ValueError)) is Cap.MONO


def test_stream_without_isatty_is_mono(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert capability(object()) is Cap.MONO  # AttributeError → non-tty


def test_capability_is_per_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert capability(_TTY) is Cap.TRUECOLOR
    assert capability(_PIPE) is Cap.MONO  # independent, re-evaluated per call


# --------------------------------------------------------------------------- #
# Task 3 — render adapters (sgr / styled / pt_style)
# --------------------------------------------------------------------------- #
def test_sgr_truecolor_introducer_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    # #7aa2f7 → 122;162;247
    assert sgr(Role.ACCENT, stream=_TTY) == "\033[38;2;122;162;247m"
    out = styled("hi", Role.ACCENT, stream=_TTY)
    assert out == "\033[38;2;122;162;247mhi\033[0m"
    assert out.endswith(RESET)


def test_sgr_256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    assert sgr(Role.ACCENT, stream=_TTY) == "\033[38;5;111m"
    assert styled("hi", Role.ACCENT, stream=_TTY) == "\033[38;5;111mhi\033[0m"


def test_sgr_mono_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert sgr(Role.ACCENT, stream=_TTY) == ""
    assert styled("hi", Role.ACCENT, stream=_TTY) == "hi"  # bare text, no escape


def test_no_escape_to_piped_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    # The cardinal sin: never emit escapes to a non-tty / piped stream.
    assert sgr(Role.ACCENT, stream=_PIPE) == ""
    assert styled("hi", Role.ACCENT, stream=_PIPE) == "hi"
    assert "\033" not in styled("hi", Role.ERROR, stream=io.StringIO())


def test_pt_style_keys() -> None:
    style = pt_style()
    assert style["class:accent"] == "#7aa2f7"
    assert style["class:text"] == "#c0caf5"
    assert set(style) == {f"class:{role.value}" for role in Role}


# --------------------------------------------------------------------------- #
# Task 5 — self-demo smoke
# --------------------------------------------------------------------------- #
def test_demo_emits_escapes_under_truecolor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")
    out = render_demo(_TTY)
    assert "\033[38;2;" in out  # at least one truecolor introducer
    assert RESET in out
    for role in Role:  # every role rendered
        assert role.value in out


def test_demo_no_escapes_under_mono(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    out = render_demo(_TTY)
    assert "\033" not in out  # mono → plain text, never an escape
    for role in Role:
        assert role.value in out
