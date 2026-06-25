"""Unit tests for the UX-4 theme-256-completeness lint (lint_theme_256).

Covers: a complete THEME passes; a missing role key, an out-of-range index, a
non-Constant (computed) index, and a 0..15 chromatic index each fire UX-4; and
the scope guard (a non-theme path is never checked).
"""

from __future__ import annotations

import ast

from scripts.check_policy_lints import THEME_MODULE, Violation, lint_theme_256

# A complete, valid THEME literal — the annotated-assign shape the real module
# ships (`THEME: Mapping[Role, Color] = MappingProxyType({...})`).
COMPLETE = """\
from types import MappingProxyType
THEME: Mapping[Role, Color] = MappingProxyType({
    Role.ACCENT: Color("#7aa2f7", 111),
    Role.SUCCESS: Color("#9ece6a", 150),
    Role.ERROR: Color("#f7768e", 210),
    Role.WARNING: Color("#e0af68", 179),
    Role.HEADING: Color("#bb9af7", 141),
    Role.IDENTIFIER: Color("#7dcfff", 117),
    Role.MUTED: Color("#9aa5ce", 146),
    Role.TEXT: Color("#c0caf5", 189),
})
"""


def _lint(src: str, path: str = THEME_MODULE) -> list[Violation]:
    return lint_theme_256(ast.parse(src), path)


def _ux4(viols: list[Violation]) -> list[Violation]:
    return [v for v in viols if v.rule_id == "UX-4"]


def test_complete_theme_passes() -> None:
    assert _lint(COMPLETE) == []


def test_unannotated_assign_form_passes() -> None:
    # Both `THEME = ...` and `THEME: Mapping[...] = ...` are recognized.
    src = COMPLETE.replace("THEME: Mapping[Role, Color] = ", "THEME = ")
    assert _lint(src) == []


def test_missing_role_fires() -> None:
    src = COMPLETE.replace('    Role.TEXT: Color("#c0caf5", 189),\n', "")
    viols = _ux4(_lint(src))
    assert len(viols) == 1
    assert "text" in viols[0].message.lower()


def test_out_of_range_index_fires() -> None:
    src = COMPLETE.replace('Color("#c0caf5", 189)', 'Color("#c0caf5", 300)')
    viols = _ux4(_lint(src))
    assert len(viols) == 1
    assert "300" in viols[0].message or "range" in viols[0].message.lower()


def test_non_constant_index_fires() -> None:
    src = COMPLETE.replace('Color("#c0caf5", 189)', 'Color("#c0caf5", 1 + 1)')
    viols = _ux4(_lint(src))
    assert len(viols) == 1


def test_chromatic_in_system_band_fires() -> None:
    # accent is chromatic → must sit in 16..231, never the palette-unstable 0..15.
    src = COMPLETE.replace('Color("#7aa2f7", 111)', 'Color("#7aa2f7", 5)')
    viols = _ux4(_lint(src))
    assert len(viols) == 1
    assert "accent" in viols[0].message.lower()


def test_non_theme_path_is_never_checked() -> None:
    # The scope guard: an incomplete THEME in any other file is ignored entirely.
    bad = "THEME = {}\n"
    assert _lint(bad, path="setforge/other.py") == []


def test_muted_text_may_sit_below_16() -> None:
    # Non-chromatic roles (muted/text) are not band-restricted (still 0..255).
    src = COMPLETE.replace('Color("#9aa5ce", 146)', 'Color("#9aa5ce", 7)')
    assert _ux4(_lint(src)) == []
