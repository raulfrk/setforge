"""Unit tests for ``_home_relative`` (setforge-5twm round-2 polish).

Covers the three boundary cases that motivated the rewrite from
``str.replace`` to ``Path.relative_to``:

1. Path under home → ``~/<rel>`` rendering.
2. Path NOT under home (and sharing a substring like ``/tmp/home/...``)
   → raw ``str(path)`` with no false-match.
3. Path exactly equal to ``Path.home()`` → bare ``~``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.cli.validate import _home_relative


def test_home_relative_under_home_collapses_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Path under ``Path.home()`` renders as ``~/<relative>``."""
    fake_home = tmp_path / "home" / "raul"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    target = fake_home / ".claude" / "CLAUDE.md"
    assert _home_relative(target) == "~/.claude/CLAUDE.md"


def test_home_relative_not_under_home_returns_raw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path that shares a substring with home (but isn't under it)
    renders untouched — guards the regression the rewrite targeted."""
    fake_home = tmp_path / "home" / "raul"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # ``/tmp/home/raul/...``-style decoy: shares ``home/raul`` as a
    # substring with ``Path.home()`` but lives elsewhere.
    decoy = tmp_path / "elsewhere" / "home" / "raul" / "file.txt"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("x\n", encoding="utf-8")
    assert _home_relative(decoy) == str(decoy)


def test_home_relative_exactly_home_renders_bare_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Path exactly equal to ``Path.home()`` renders as ``~`` (not ``~/.``)."""
    fake_home = tmp_path / "home" / "raul"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    assert _home_relative(fake_home) == "~"
