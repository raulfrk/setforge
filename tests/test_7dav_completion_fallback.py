"""Static-template fallback for completion when schema walk fails.

Anti-smell #17: a transient config-parse failure must NEVER break the
shell. ``_complete_path_dispatch`` wraps the schema walk in a broad
``except`` that falls back to a small static list of top-level keys.
"""

from __future__ import annotations

from typing import Any

import pytest

from setforge.cli.config import (
    ConfigScope,
    _complete_path_dispatch,
    _static_template_paths,
)


class _FakeCtx:
    def __init__(self, *, local: bool = True) -> None:
        self.params: dict[str, Any] = {"local": local, "tracked": not local}
        self.info_name = "show"


def test_static_local_template_lists_top_level_keys() -> None:
    """The static fallback list carries the local-scope top-level keys."""
    fallback = _static_template_paths(ConfigScope.LOCAL)
    assert "source" in fallback
    assert "binaries" in fallback


def test_static_tracked_template_lists_top_level_keys() -> None:
    """The static fallback list carries the tracked-scope top-level keys."""
    fallback = _static_template_paths(ConfigScope.TRACKED)
    assert "tracked_files" in fallback
    assert "profiles" in fallback


def test_dispatch_falls_back_on_schema_walk_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_complete_path_local`` raises, the dispatch falls back."""

    def _explode(ctx: Any, incomplete: str) -> list[str]:
        raise RuntimeError("schema walk exploded")

    monkeypatch.setattr("setforge.cli.config._complete_path_local", _explode)
    result = _complete_path_dispatch(_FakeCtx(local=True), "")
    # Fallback list arrives instead of an exception.
    assert "source" in result
    assert "binaries" in result
