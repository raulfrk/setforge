"""Unit tests for the Tokyo Night theme module (setforge/ui/theme.py)."""

from __future__ import annotations

import dataclasses

import pytest

from setforge.ui.theme import THEME, Cap, Color, Role, capability

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
