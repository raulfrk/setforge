"""Tokyo Night theme — semantic roles, truecolor + curated 256 fallback.

The project's single visual source of truth (RFC §8): a small set of *semantic*
colour roles (accent / success / error / ... — never raw hex at call sites),
each carrying a truecolor ``#rrggbb`` and a curated official xterm-256 index for
the fallback path. Dark-only; no light variant, no user overrides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Role(StrEnum):
    """A semantic colour role — the only colour vocabulary call sites may use."""

    ACCENT = "accent"
    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    HEADING = "heading"
    IDENTIFIER = "identifier"
    MUTED = "muted"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Color:
    """One role's colour: a truecolor hex + a curated official xterm-256 index."""

    truecolor: str  # "#rrggbb"
    xterm256: int  # curated official index, 0..255 (chromatic roles 16..231)


# Truecolor values are the canonical Tokyo Night palette (RFC §8.2). The 256
# indices are curated official xterm approximations of each truecolor — chromatic
# roles pinned inside the 16..231 colour cube (the 0..15 system slots are
# palette-unstable across terminals). Validity + band are enforced by UX-4.
THEME: Mapping[Role, Color] = MappingProxyType(
    {
        Role.ACCENT: Color("#7aa2f7", 111),
        Role.SUCCESS: Color("#9ece6a", 150),
        Role.ERROR: Color("#f7768e", 210),
        Role.WARNING: Color("#e0af68", 179),
        Role.HEADING: Color("#bb9af7", 141),
        Role.IDENTIFIER: Color("#7dcfff", 117),
        Role.MUTED: Color("#9aa5ce", 146),
        Role.TEXT: Color("#c0caf5", 189),
    }
)
