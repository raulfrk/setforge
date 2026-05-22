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
from typing import Any, cast

import pytest
import typer

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
    assert _complete_value(cast(typer.Context, ctx), "") == []


def test_value_completion_enum_yields_members() -> None:
    """A ``StrEnum`` scalar surfaces the enum members.

    ``source.kind`` is constrained to ``path`` | ``git`` via
    :class:`setforge.source.SourceKind`. The contract is that both
    members surface as completion candidates — an empty list satisfies
    ``isinstance(list)`` but is useless for the user, so the assertion
    pins both expected values explicitly.
    """
    ctx = _FakeCtx(path="source.kind")
    suggestions = _complete_value(cast(typer.Context, ctx), "")
    assert "path" in suggestions, suggestions
    assert "git" in suggestions, suggestions


def test_value_completion_with_empty_path_yields_empty() -> None:
    """No path argument means no value suggestion possible."""
    ctx = _FakeCtx(path=None)
    assert _complete_value(cast(typer.Context, ctx), "") == []


def test_value_completion_empty_for_scalar_with_no_enum() -> None:
    """Scalar fields without an enum surface an empty completion list.

    ``binaries.code`` is a free-form ``Path``-typed scalar (no
    ``StrEnum`` / ``Literal`` constraint), so the
    ``_complete_value_impl`` walk falls through the ``node.is_list``
    branch and the ``node.enum_values`` branch and returns ``[]``.
    Pinning this contract guards against accidentally surfacing
    irrelevant universe values (e.g., from a misrouted enum lookup)
    for free-form scalars.
    """
    ctx = _FakeCtx(path="binaries.code")
    out = _complete_value(cast(typer.Context, ctx), "")
    assert out == []
