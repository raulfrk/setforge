"""SetForge shared UI package: theme + reusable widgets.

Re-exports the button-bar widget surface (``Button``, ``button_bar``,
``CANCEL``); the theme symbols are re-exported from :mod:`setforge.ui.theme`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from setforge.ui.primitives import CANCEL, Button
from setforge.ui.theme import (
    THEME,
    Cap,
    Color,
    Role,
    capability,
    pt_style,
    sgr,
    styled,
)

if TYPE_CHECKING:
    from setforge.ui.widgets import button_bar


def __getattr__(name: str) -> Any:  # noqa: ANN401 - lazy public re-export
    if name == "button_bar":
        from setforge.ui.widgets import button_bar

        return button_bar
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CANCEL",
    "THEME",
    "Button",
    "Cap",
    "Color",
    "Role",
    "button_bar",
    "capability",
    "pt_style",
    "sgr",
    "styled",
]
