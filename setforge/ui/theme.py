"""Tokyo Night theme — semantic roles, truecolor + curated 256 fallback.

The project's single visual source of truth (RFC §8): a small set of *semantic*
colour roles (accent / success / error / ... — never raw hex at call sites),
each carrying a truecolor ``#rrggbb`` and a curated official xterm-256 index for
the fallback path. Dark-only; no light variant, no user overrides.

Two render adapters project the one :data:`THEME` table onto the two colour
surfaces the project uses — prompt_toolkit (:func:`pt_style`) and raw SGR
escapes (:func:`sgr` / :func:`styled`) — and their formats are never crossed
(a prompt_toolkit style string is never fed to a raw SGR path, and vice versa).

Capability is resolved *per destination stream*, re-evaluated on every call
(:func:`capability`): ``NO_COLOR`` (set and non-empty) or a non-tty stream
forces mono; ``COLORTERM`` ∈ {``truecolor``, ``24bit``} unlocks truecolor; every
other tty falls back to 256. Escapes are NEVER emitted to a piped / non-tty
stream — the adapters degrade to plain text instead.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

RESET = "\033[0m"  # SGR reset; every emitted sgr() run is paired with this


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


class Cap(StrEnum):
    """A stream's resolved colour capability."""

    MONO = "mono"
    C256 = "256"
    TRUECOLOR = "truecolor"


def _env_set(name: str) -> bool:
    """An env var is "set" only when present AND non-empty.

    Per no-color.org, ``NO_COLOR=""`` (empty) does NOT disable colour — only a
    non-empty value does.
    """
    return name in os.environ and os.environ[name] != ""


def _isatty(stream: object) -> bool:
    """``stream.isatty()`` guarded against closed / detached / non-stream objects.

    A missing ``isatty`` (``AttributeError``), a closed stream (``ValueError``),
    or an OS-level failure (``OSError``) all resolve to "not a tty".
    """
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (ValueError, OSError, AttributeError):
        return False


def capability(stream: object) -> Cap:
    """Resolve a stream's colour capability — first match wins, per call.

    Precedence: ``NO_COLOR`` (set + non-empty) → mono; non-tty stream → mono;
    ``COLORTERM`` ∈ {``truecolor``, ``24bit``} (exact membership) → truecolor;
    otherwise 256. No module-level cache — cheap to re-evaluate per stream.

    ``TERM`` (incl. ``TERM=dumb`` / unset) is intentionally NOT consulted — a tty
    with ``COLORTERM`` truecolor still gets truecolor; everything else falls to
    the 256 floor.
    """
    if _env_set("NO_COLOR"):
        return Cap.MONO
    if not _isatty(stream):
        return Cap.MONO
    if os.environ.get("COLORTERM") in {"truecolor", "24bit"}:
        return Cap.TRUECOLOR
    return Cap.C256


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """``"#rrggbb"`` → ``(r, g, b)`` ints."""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def sgr(role: Role, *, stream: object) -> str:
    """Raw SGR colour introducer for ``role`` on ``stream`` (no reset, no text).

    Truecolor → ``\\033[38;2;r;g;bm``; 256 → ``\\033[38;5;Nm``; mono → ``""``.
    Returns ``""`` for any non-tty / ``NO_COLOR`` stream so callers never leak
    escapes into a pipe. Pair the result with :data:`RESET` (or use
    :func:`styled`).
    """
    color = THEME[role]
    cap = capability(stream)
    if cap is Cap.TRUECOLOR:
        r, g, b = _hex_to_rgb(color.truecolor)
        return f"\033[38;2;{r};{g};{b}m"
    if cap is Cap.C256:
        return f"\033[38;5;{color.xterm256}m"
    return ""  # Cap.MONO


def styled(text: str, role: Role, *, stream: object) -> str:
    """``text`` wrapped in ``role``'s SGR introducer + :data:`RESET`.

    Degrades to bare ``text`` (no escapes) when the stream resolves to mono, so
    it is always safe to write to a pipe.
    """
    introducer = sgr(role, stream=stream)
    if not introducer:
        return text
    return f"{introducer}{text}{RESET}"


def pt_style(theme: Mapping[Role, Color] = THEME) -> dict[str, str]:
    """A prompt_toolkit ``Style`` mapping keyed ``class:<role>`` → truecolor hex.

    prompt_toolkit resolves its own 256/mono downgrade from the terminal, so the
    pt adapter always hands it the truecolor hex (never the rich ``color(N)`` /
    raw SGR forms — adapter formats are never crossed).
    """
    return {f"class:{role.value}": color.truecolor for role, color in theme.items()}


def render_demo(stream: object) -> str:
    """The self-demo body: every role rendered as a styled sample line for
    ``stream``. Returned (not printed) so it is unit-testable; ``_main`` writes
    it. Resolves colour from ``stream`` like every other adapter call."""
    lines = [
        styled(
            f"  {role.value:<12} sample text — {THEME[role].truecolor}",
            role,
            stream=stream,
        )
        for role in Role
    ]
    return "\n".join(lines) + "\n"


def _main() -> None:
    """``python -m setforge.ui.theme`` — print the palette demo to stdout."""
    import sys

    sys.stdout.write(render_demo(sys.stdout))


if __name__ == "__main__":
    _main()
