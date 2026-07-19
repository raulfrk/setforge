"""Static-template fallback for completion when schema walk fails.

Anti-smell #17: a transient config-parse failure must NEVER break the
shell. ``_complete_path_dispatch`` wraps the schema walk in a broad
``except`` that falls back to a small static list of top-level keys.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
import typer

from setforge.cli.config import (
    ConfigScope,
    _complete_path_dispatch,
    _static_template_paths,
)
from setforge.config import Config
from setforge.errors import SetforgeError
from setforge.local_config import LocalConfig


class _FakeCtx:
    def __init__(self, *, local: bool = True) -> None:
        self.params: dict[str, Any] = {"local": local, "tracked": not local}
        self.info_name = "show"


def test_static_local_template_matches_model_fields() -> None:
    """The local fallback list is exactly ``LocalConfig``'s top-level keys.

    Pins the fallback to the model so it can never silently drift from
    the real config schema — the drift the schema-derived fix retired.
    """
    assert _static_template_paths(ConfigScope.LOCAL) == list(LocalConfig.model_fields)


def test_static_tracked_template_matches_model_fields() -> None:
    """The tracked fallback list is exactly ``Config``'s top-level keys."""
    assert _static_template_paths(ConfigScope.TRACKED) == list(Config.model_fields)


def test_dispatch_falls_back_on_schema_walk_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_complete_path_local`` raises a plausible failure, dispatch falls back.

    The dispatch's narrowed catch covers the realistic schema-walk
    failure families (SetforgeError, KeyError, AttributeError,
    ValueError, OSError) — exercise SetforgeError here since that's the
    family the runtime path raises on malformed config.
    """

    def _explode(ctx: Any, incomplete: str) -> list[str]:
        raise SetforgeError("schema walk exploded")

    monkeypatch.setattr("setforge.cli.config._complete_path_local", _explode)
    result = _complete_path_dispatch(cast(typer.Context, _FakeCtx(local=True)), "")
    # Fallback list arrives instead of an exception.
    assert "source" in result
    assert "binaries" in result
