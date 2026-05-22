"""Path-completion callbacks for ``setforge config`` (schema-driven).

Each ``autocompletion=`` callback walks the cached schema and yields
dotted paths matching the user's incomplete prefix.
"""

from __future__ import annotations

from typing import Any, cast

import typer

from setforge.cli.config import (
    ConfigScope,
    _complete_path_local,
    _complete_path_tracked,
    _enumerate_paths,
)


class _FakeCtx:
    """Minimal stand-in for :class:`typer.Context` in completion tests."""

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}
        self.info_name: str | None = None


def test_local_path_completion_includes_source_kind() -> None:
    """``source.kind`` is reachable via the local schema walk."""
    paths = _enumerate_paths(ConfigScope.LOCAL)
    assert "source" in paths
    # Nested through PathSource / GitSource union.
    assert any(p.startswith("source.") for p in paths)


def test_local_path_completion_filters_by_prefix() -> None:
    """``_complete_path_local`` filters by ``incomplete`` prefix."""
    suggestions = _complete_path_local(cast(typer.Context, _FakeCtx()), "sourc")
    assert all(s.startswith("sourc") for s in suggestions)
    assert "source" in suggestions


def test_tracked_path_completion_yields_top_level_keys() -> None:
    """``Config`` walk surfaces ``profiles`` and ``tracked_files``."""
    suggestions = _complete_path_tracked(cast(typer.Context, _FakeCtx()), "")
    assert "profiles" in suggestions
    assert "tracked_files" in suggestions
    assert "marketplaces" in suggestions
    assert "claude_plugins" in suggestions


def test_tracked_path_completion_yields_nested_paths() -> None:
    """Nested paths like ``tracked_files.<x>.src`` are enumerable."""
    paths = _enumerate_paths(ConfigScope.TRACKED)
    # tracked_files: dict[str, TrackedFile] → tracked_files.src must
    # show up under the recursive dict-value walk.
    assert any(p.startswith("tracked_files.") for p in paths)
