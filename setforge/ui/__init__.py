"""SetForge shared UI package: theme + reusable widgets.

This module re-exports the D2 button-bar surface. D1's theme symbols
(``Role``, ``THEME``, ``pt_style``, ``rich_style``, ``sgr``) are added by the
sibling theme worktree; the two ``__init__`` exports union at the merge gate.
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
