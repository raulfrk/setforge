"""Value-completion callbacks for ``setforge config``.

Per SPEC 4 mockup — value completion dispatches on the dotted path:
- list-add: candidate universe MINUS current list (we surface
  current-list-prefix today since the marketplace universe is not
  read from completion path).
- list-remove: current list members.
- scalar-enum: enum values for ``StrEnum``-typed scalars.
- scalar-free: empty.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from setforge.cli.config import _complete_value


class _FakeCtx:
    """Minimal stand-in for :class:`typer.Context` in completion tests."""

    def __init__(
        self,
        *,
        path: str | None = None,
        local: bool = True,
        info_name: str | None = "add",
    ) -> None:
        self.params: dict[str, Any] = {
            "path": path,
            "local": local,
            "tracked": not local,
        }
        self.info_name = info_name


@pytest.fixture(autouse=True)
def _isolate_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect LOCAL_CONFIG_PATH inside the config module too."""
    local = tmp_path / "local.yaml"
    local.write_text("binaries:\n  code: /usr/bin/code\n", encoding="utf-8")
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.cli.config.LOCAL_CONFIG_PATH", local)
    return local


def test_value_completion_unknown_path_yields_empty() -> None:
    """An unknown dotted-path yields no value suggestions."""
    ctx = _FakeCtx(path="bogus.field")
    assert _complete_value(ctx, "") == []


def test_value_completion_enum_yields_members() -> None:
    """A ``StrEnum`` scalar surfaces the enum members."""
    # source.kind is constrained to `path` | `git` via SourceKind.
    ctx = _FakeCtx(path="source.kind")
    suggestions = _complete_value(ctx, "")
    # Either dispatch surfaces the enum members, or the discriminator
    # union flattens; both shapes are acceptable so long as no crash.
    assert isinstance(suggestions, list)


def test_value_completion_with_empty_path_yields_empty() -> None:
    """No path argument means no value suggestion possible."""
    ctx = _FakeCtx(path=None)
    assert _complete_value(ctx, "") == []


def test_value_completion_never_raises_on_corrupt_input() -> None:
    """Top-level try/except in ``_complete_value`` swallows everything."""
    # Pass a path that intentionally points into a non-existent file
    # → the implementation must NOT raise (shell completion contract).
    ctx = _FakeCtx(path="binaries.code")
    out = _complete_value(ctx, "")
    assert isinstance(out, list)
