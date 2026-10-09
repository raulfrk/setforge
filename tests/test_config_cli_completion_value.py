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
from typing import cast

import pytest
import typer

from setforge.cli.config import _complete_value
from tests.fakes import FakeCtx


@pytest.fixture(autouse=True)
def _isolate_local(tmp_path: Path) -> Path:
    """Seed the per-test ``local.yaml`` with one binary override."""
    local = tmp_path / "local.yaml"
    local.write_text("binaries:\n  code: /usr/bin/code\n", encoding="utf-8")
    return local


def test_value_completion_unknown_path_yields_empty() -> None:
    """An unknown dotted-path yields no value suggestions."""
    ctx = FakeCtx(path="bogus.field")
    assert _complete_value(cast(typer.Context, ctx), "") == []


def test_value_completion_enum_yields_members() -> None:
    """A ``StrEnum`` scalar surfaces the enum members.

    ``source.kind`` is constrained to ``path`` | ``git`` via
    :class:`setforge.source.SourceKind`. The contract is that both
    members surface as completion candidates — an empty list satisfies
    ``isinstance(list)`` but is useless for the user, so the assertion
    pins both expected values explicitly.
    """
    ctx = FakeCtx(path="source.kind")
    suggestions = _complete_value(cast(typer.Context, ctx), "")
    assert "path" in suggestions, suggestions
    assert "git" in suggestions, suggestions


def test_value_completion_with_empty_path_yields_empty() -> None:
    """No path argument means no value suggestion possible."""
    ctx = FakeCtx(path=None)
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
    ctx = FakeCtx(path="binaries.code")
    out = _complete_value(cast(typer.Context, ctx), "")
    assert out == []


@pytest.mark.parametrize("path", ["plugins.remove", "extensions.remove"])
def test_value_completion_remove_lists_overlay_entries(
    tmp_path: Path, path: str
) -> None:
    """``config remove --local plugins.remove <TAB>`` offers the listed entries."""
    block, leaf = path.split(".")
    (tmp_path / "local.yaml").write_text(
        f"{block}:\n  {leaf}:\n    - alpha\n    - beta\n", encoding="utf-8"
    )
    ctx = FakeCtx(path=path, info_name="remove")

    assert _complete_value(cast(typer.Context, ctx), "a") == ["alpha"]
