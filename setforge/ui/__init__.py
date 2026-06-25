"""SetForge UI layer — the shared Tokyo Night theme + button-bar widget.

This package re-exports the stable public surface so call sites import from
``setforge.ui`` rather than reaching into the submodules.
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
]
