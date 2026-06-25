"""Unit tests for the Tokyo Night theme module (setforge/ui/theme.py)."""

from __future__ import annotations

import dataclasses

import pytest

from setforge.ui.theme import THEME, Color, Role

CHROMATIC = (
    Role.ACCENT,
    Role.SUCCESS,
    Role.ERROR,
    Role.WARNING,
    Role.HEADING,
    Role.IDENTIFIER,
)


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
