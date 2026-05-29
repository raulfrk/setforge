"""Exhaustiveness-guard coverage for the two ``display_tag`` match functions.

Asserts each function maps every existing enum variant and carries an
``assert_never`` guard in a ``case _`` arm, so a future variant becomes a
type + runtime error rather than an implicit ``None`` return.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any

import pytest

from setforge import local_overlay, preserved_keys
from setforge.local_overlay import OverlayOrigin
from setforge.local_overlay import display_tag as overlay_display_tag
from setforge.preserved_keys import (
    KeyOrigin,
    ResolvedPreservedKey,
)
from setforge.preserved_keys import display_tag as key_display_tag


def test_key_display_tag_maps_every_variant() -> None:
    """Every :class:`KeyOrigin` member produces a non-empty tag."""
    expected = {
        KeyOrigin.FROM_PROFILE: "[from profile p]",
        KeyOrigin.FROM_LOCAL_YAML: "[from local.yaml]",
        KeyOrigin.REMOVED_VIA_LOCAL: "[removed via local.yaml]",
    }
    assert set(expected) == set(KeyOrigin)
    for origin, tag in expected.items():
        resolved = ResolvedPreservedKey(key="k", origin=origin, source_profile="p")
        assert key_display_tag(resolved) == tag


def test_overlay_display_tag_maps_every_variant() -> None:
    """Every :class:`OverlayOrigin` member produces the expected tag."""
    expected = {
        OverlayOrigin.LOCAL_ADD: "[from local.yaml]",
        OverlayOrigin.LOCAL_REMOVE: "[− removed via local.yaml]",  # noqa: RUF001
        OverlayOrigin.PROFILE: "",
    }
    assert set(expected) == set(OverlayOrigin)
    for origin, tag in expected.items():
        assert overlay_display_tag(origin) == tag


def _last_case_calls_assert_never(func: Any) -> bool:
    """Return True iff ``func`` ends its match with ``case _`` -> assert_never.

    Parses the function source, finds the (single) ``match`` statement, and
    confirms the final case is a wildcard whose only body statement is a call
    to ``assert_never`` — and that no statement follows the match (which would
    permit an implicit ``None`` fall-through past the guard).
    """
    source = inspect.getsource(func)
    tree = ast.parse(textwrap.dedent(source))
    func_def = tree.body[0]
    assert isinstance(func_def, ast.FunctionDef)
    match_stmts = [s for s in func_def.body if isinstance(s, ast.Match)]
    assert len(match_stmts) == 1, "expected exactly one match statement"
    match_stmt = match_stmts[0]
    # The match must be the final statement: nothing may follow it.
    assert func_def.body[-1] is match_stmt, "match must be the last statement"
    last_case = match_stmt.cases[-1]
    # Wildcard arm. `case _ as unreachable` parses as a MatchAs binding
    # (name="unreachable") whose inner pattern is the bare wildcard — itself
    # a MatchAs with pattern=None. A plain `case _:` is a single MatchAs with
    # pattern=None. Accept both: the arm must ultimately be a catch-all.
    pattern = last_case.pattern
    if not isinstance(pattern, ast.MatchAs):
        return False
    inner = pattern.pattern
    is_catch_all = inner is None or (
        isinstance(inner, ast.MatchAs) and inner.pattern is None
    )
    if not is_catch_all:
        return False
    if len(last_case.body) != 1:
        return False
    stmt = last_case.body[0]
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    callee = stmt.value.func
    return isinstance(callee, ast.Name) and callee.id == "assert_never"


@pytest.mark.parametrize(
    "func",
    [key_display_tag, overlay_display_tag],
    ids=["preserved_keys", "local_overlay"],
)
def test_display_tag_has_assert_never_guard(func: Any) -> None:
    """Both ``display_tag`` functions terminate the match with assert_never."""
    assert _last_case_calls_assert_never(func)


@pytest.mark.parametrize(
    "module",
    [preserved_keys, local_overlay],
    ids=["preserved_keys", "local_overlay"],
)
def test_assert_never_imported_from_typing(module: Any) -> None:
    """The guard helper comes from ``typing`` (not ``typing_extensions``)."""
    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "typing"
            and any(alias.name == "assert_never" for alias in node.names)
        ):
            found = True
    assert found, "assert_never must be imported from typing"


def test_forced_unknown_key_origin_trips_guard() -> None:
    """A value outside the enum reaches ``case _`` and raises (no None return).

    ``assert_never`` raises ``AssertionError`` at runtime when actually
    reached, so a forced unknown origin must raise rather than fall through
    to an implicit ``None``.
    """
    resolved = ResolvedPreservedKey.__new__(ResolvedPreservedKey)
    object.__setattr__(resolved, "key", "k")
    object.__setattr__(resolved, "origin", "totally-unknown-origin")
    object.__setattr__(resolved, "source_profile", "p")
    with pytest.raises(AssertionError):
        key_display_tag(resolved)


def test_forced_unknown_overlay_origin_trips_guard() -> None:
    """A value outside :class:`OverlayOrigin` raises rather than returning None."""
    with pytest.raises(AssertionError):
        overlay_display_tag("totally-unknown-origin")  # type: ignore[arg-type]
