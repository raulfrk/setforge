"""Tests for the interactive conflict wizard resolver.

Drives :func:`setforge.conflict_wizard.make_wizard_resolver` against a
line-based (:class:`~setforge.markdown_merge.LineConflict`) and a structural
(:class:`~setforge.structural_merge.PathConflict`) conflict. Keypresses are
supplied either via the non-tty line-buffered fallback of
:func:`setforge.wizard.read_one_choice` (a ``StringIO`` on ``sys.stdin``) or by
monkeypatching ``read_one_choice`` directly; ``$EDITOR`` is monkeypatched to a
function that writes known content into the tmpfile.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest

from setforge import conflict_wizard
from setforge.disposition_merge import ConflictChoice
from setforge.markdown_merge import LineConflict
from setforge.scalar_merge import ABSENT, ScalarConflict
from setforge.structural_merge import PathConflict

_LINE = LineConflict(base=["base\n"], ours=["live\n"], theirs=["tracked\n"])
_PATH = PathConflict(path="a.b", base=1, ours=2, theirs=3)
_PATH_ABSENT = PathConflict(path="a.b", base=1, ours=ABSENT, theirs=3)
_SCALAR = ScalarConflict(path="a.k", base=1, ours=7, theirs=8)
_SCALAR_ABSENT = ScalarConflict(path="a.k", base=1, ours=ABSENT, theirs=8)


def _feed_keys(monkeypatch: pytest.MonkeyPatch, keys: str) -> None:
    """Route ``read_one_choice`` through its non-tty stdin fallback for ``keys``."""
    monkeypatch.setattr("sys.stdin", io.StringIO(keys))


def _patch_choice(monkeypatch: pytest.MonkeyPatch, keys: list[str]) -> None:
    """Replace ``read_one_choice`` with one that pops ``keys`` in order."""
    it: Iterator[str] = iter(keys)

    def _fake(prompt: str, choices: set[str]) -> str:
        return next(it)

    monkeypatch.setattr(conflict_wizard, "read_one_choice", _fake)


def _patch_editor(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    """Replace ``run_editor`` with one that writes ``content`` to the tmpfile."""

    def _fake(target: Path) -> None:
        target.write_text(content, encoding="utf-8")

    monkeypatch.setattr(conflict_wizard, "run_editor", _fake)


# ---------------------------------------------------------------------------
# Keypress -> choice mapping (both conflict kinds), via the stdin fallback.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "choice"),
    [
        ("k", ConflictChoice.KEEP_OURS),
        ("t", ConflictChoice.TAKE_THEIRS),
        ("s", ConflictChoice.SKIP),
    ],
)
def test_line_conflict_keypress_maps(
    monkeypatch: pytest.MonkeyPatch, key: str, choice: ConflictChoice
) -> None:
    _feed_keys(monkeypatch, key)
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_LINE)
    assert res.choice is choice
    assert res.edited_lines is None
    assert res.edited_value is None


@pytest.mark.parametrize(
    ("key", "choice"),
    [
        ("k", ConflictChoice.KEEP_OURS),
        ("t", ConflictChoice.TAKE_THEIRS),
        ("s", ConflictChoice.SKIP),
    ],
)
def test_path_conflict_keypress_maps(
    monkeypatch: pytest.MonkeyPatch, key: str, choice: ConflictChoice
) -> None:
    _feed_keys(monkeypatch, key)
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_PATH)
    assert res.choice is choice
    assert res.edited_value is None


@pytest.mark.parametrize(
    ("key", "choice"),
    [
        ("k", ConflictChoice.KEEP_OURS),
        ("t", ConflictChoice.TAKE_THEIRS),
        ("s", ConflictChoice.SKIP),
    ],
)
def test_scalar_conflict_keypress_maps(
    monkeypatch: pytest.MonkeyPatch, key: str, choice: ConflictChoice
) -> None:
    _feed_keys(monkeypatch, key)
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_SCALAR)
    assert res.choice is choice
    assert res.edited_value is None


# ---------------------------------------------------------------------------
# EDIT produces edited_lines / edited_value.
# ---------------------------------------------------------------------------


def test_line_conflict_edit_produces_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_choice(monkeypatch, ["e"])
    _patch_editor(monkeypatch, "edited-one\nedited-two\n")
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_LINE)
    assert res.choice is ConflictChoice.EDIT
    assert res.edited_lines == ["edited-one\n", "edited-two\n"]


def test_path_conflict_edit_produces_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_choice(monkeypatch, ["e"])
    _patch_editor(monkeypatch, "42\n")
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_PATH)
    assert res.choice is ConflictChoice.EDIT
    assert res.edited_value == 42


def test_path_conflict_edit_roundtrips_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_choice(monkeypatch, ["e"])
    _patch_editor(monkeypatch, "x: 1\ny: two\n")
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_PATH)
    assert res.choice is ConflictChoice.EDIT
    assert res.edited_value == {"x": 1, "y": "two"}


def test_path_conflict_edit_absent_ours_seeds_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ABSENT ours seeds an empty buffer; the editor supplies the new value."""
    seen: dict[str, str] = {}

    def _fake(target: Path) -> None:
        seen["seed"] = target.read_text(encoding="utf-8")
        target.write_text("new\n", encoding="utf-8")

    _patch_choice(monkeypatch, ["e"])
    monkeypatch.setattr(conflict_wizard, "run_editor", _fake)
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_PATH_ABSENT)
    assert seen["seed"] == ""
    assert res.edited_value == "new"


def test_scalar_conflict_edit_produces_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_choice(monkeypatch, ["e"])
    _patch_editor(monkeypatch, "99\n")
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_SCALAR)
    assert res.choice is ConflictChoice.EDIT
    assert res.edited_value == 99


def test_scalar_conflict_edit_string_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_choice(monkeypatch, ["e"])
    _patch_editor(monkeypatch, "hello\n")
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_SCALAR)
    assert res.choice is ConflictChoice.EDIT
    assert res.edited_value == "hello"


def test_scalar_conflict_edit_absent_ours_seeds_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ABSENT ours seeds an empty buffer; the editor supplies the new scalar."""
    seen: dict[str, str] = {}

    def _fake(target: Path) -> None:
        seen["seed"] = target.read_text(encoding="utf-8")
        target.write_text("new\n", encoding="utf-8")

    _patch_choice(monkeypatch, ["e"])
    monkeypatch.setattr(conflict_wizard, "run_editor", _fake)
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_SCALAR_ABSENT)
    assert seen["seed"] == ""
    assert res.edited_value == "new"


# ---------------------------------------------------------------------------
# Scalar EDIT rejects a non-scalar result (mapping / list) and re-prompts.
# ---------------------------------------------------------------------------


def test_scalar_conflict_edit_rejects_mapping_reprompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit producing a mapping is rejected; a second 'k' resolves."""
    _patch_choice(monkeypatch, ["e", "k"])

    calls: list[int] = []

    def _fake(target: Path) -> None:
        calls.append(1)
        target.write_text("x: 1\ny: 2\n", encoding="utf-8")

    monkeypatch.setattr(conflict_wizard, "run_editor", _fake)
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_SCALAR)
    # Editor ran once (the rejected mapping edit); re-prompt chose keep-ours.
    assert calls == [1]
    assert res.choice is ConflictChoice.KEEP_OURS


def test_scalar_conflict_edit_rejects_list_reprompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit producing a list is rejected; a second 't' resolves."""
    _patch_choice(monkeypatch, ["e", "t"])

    calls: list[int] = []

    def _fake(target: Path) -> None:
        calls.append(1)
        target.write_text("- 1\n- 2\n", encoding="utf-8")

    monkeypatch.setattr(conflict_wizard, "run_editor", _fake)
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_SCALAR)
    assert calls == [1]
    assert res.choice is ConflictChoice.TAKE_THEIRS


def test_scalar_conflict_edit_parse_error_reprompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first edit that fails to parse re-prompts; a second 'k' resolves."""
    _patch_choice(monkeypatch, ["e", "k"])

    calls: list[int] = []

    def _fake(target: Path) -> None:
        calls.append(1)
        target.write_text("{a: 1, b:\n", encoding="utf-8")

    monkeypatch.setattr(conflict_wizard, "run_editor", _fake)
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_SCALAR)
    assert calls == [1]
    assert res.choice is ConflictChoice.KEEP_OURS


# ---------------------------------------------------------------------------
# Structural edit parse error re-prompts.
# ---------------------------------------------------------------------------


def test_path_conflict_edit_parse_error_reprompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first edit that fails to parse re-prompts; a second 'k' resolves."""
    _patch_choice(monkeypatch, ["e", "k"])

    calls: list[int] = []

    def _fake(target: Path) -> None:
        calls.append(1)
        # Unbalanced flow mapping → YAML parse error.
        target.write_text("{a: 1, b:\n", encoding="utf-8")

    monkeypatch.setattr(conflict_wizard, "run_editor", _fake)
    resolver = conflict_wizard.make_wizard_resolver()
    res = resolver(_PATH)
    # Editor ran once (the bad edit); then re-prompt chose keep-ours.
    assert calls == [1]
    assert res.choice is ConflictChoice.KEEP_OURS


def test_make_wizard_resolver_returns_callable() -> None:
    resolver = conflict_wizard.make_wizard_resolver()
    assert callable(resolver)
