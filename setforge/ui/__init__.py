"""SetForge shared UI package: theme + reusable widgets.

Re-exports the button-bar widget surface (``Button``, ``button_bar``,
``CANCEL``); the theme symbols are re-exported from :mod:`setforge.ui.theme`.
"""

from __future__ import annotations

from setforge.ui.theme import (
    THEME,
    Cap,
    Color,
    Role,
    capability,
    pt_style,
    rich_style,
    sgr,
    styled,
)
from setforge.ui.widgets import CANCEL, Button, button_bar

__all__ = [
    "THEME",
    "Cap",
    "Color",
    "Role",
    "capability",
    "pt_style",
    "rich_style",
    "sgr",
    "styled",
    "CANCEL",
    "Button",
    "button_bar",
]
