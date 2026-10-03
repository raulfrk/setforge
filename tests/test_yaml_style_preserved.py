"""Programmatic edits keep the config file's own indentation, EOL and BOM."""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.vscode_extensions import add_to_include

_BASE = """\
version: 1

# Packages comment.
packages:
  keep.me:
    type: extension
    extension: keep.me

tracked_files:
  d:
    src: x
    dst: y

profiles:
  base:
    tracked_files:
      - d
    packages:
      - keep.me  # pinned
"""

_FOUR = """\
version: 1

packages:
    keep.me:
        type: extension
        extension: keep.me

tracked_files:
    d:
        src: x
        dst: y   # live

profiles:
    base:
        tracked_files:
            - d
        packages:
            - keep.me
"""

_FLAT_SEQ = """\
version: 1

packages:
  keep.me:
    type: extension
    extension: keep.me

tracked_files:
  d:
    src: x
    dst: y

profiles:
  base:
    tracked_files:
    - d
    packages:
    - keep.me
"""

_MIXED = """\
version: 1

packages:
  keep.me:
    type: extension
    extension: keep.me

tracked_files:
    d:
        src: x
        dst: y   # live

profiles:
  base:
    tracked_files:
    - d
    packages:
      - keep.me
"""


def _changed_lines(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(
            before.splitlines(), after.splitlines(), lineterm="", n=0
        )
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]


_STYLES = [
    _BASE,
    _FOUR,
    _FLAT_SEQ,
    _MIXED,
    _BASE.replace("\n", "\r\n"),
    "\ufeff" + _BASE,
]
_IDS = ["two", "four", "flat-seq", "mixed", "crlf", "bom"]


@pytest.mark.parametrize("text", _STYLES, ids=_IDS)
def test_ext_add_changes_only_added_lines(tmp_path: Path, text: str) -> None:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_bytes(text.encode("utf-8"))

    assert add_to_include(cfg, "base", "new.one")

    added = cfg.read_bytes().decode("utf-8")
    assert added.startswith("\ufeff") == text.startswith("\ufeff")
    assert ("\r\n" in added) == ("\r\n" in text)
    assert all(line.startswith("+") for line in _changed_lines(text, added))


_LOCAL = """\
# host overrides
source:
{i}kind: path
{i}path: /opt/cfg  # inline
binaries:
{i}code: /usr/bin/code  # mine

{i}# patch is special
{i}patch: /usr/bin/patch
"""


@pytest.mark.parametrize("indent", ["  ", "    ", "   "], ids=["two", "four", "three"])
def test_config_add_then_remove_restores_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, indent: str
) -> None:
    local = tmp_path / "local.yaml"
    local.write_text(_LOCAL.format(i=indent), encoding="utf-8")
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.cli.config.LOCAL_CONFIG_PATH", local)
    before = local.read_bytes()
    runner = CliRunner()

    added = runner.invoke(
        app, ["config", "add", "--local", "binaries.tool", "/opt/tool", "--yes"]
    )
    assert added.exit_code == 0, added.stdout + added.stderr
    assert all(
        line.startswith("+")
        for line in _changed_lines(before.decode(), local.read_text(encoding="utf-8"))
    )

    removed = runner.invoke(
        app, ["config", "remove", "--local", "binaries.tool", "--yes"]
    )
    assert removed.exit_code == 0, removed.stdout + removed.stderr
    assert local.read_bytes() == before


_BEFORE_NEXT = """\
binaries:
  code: /a
  claude: /b  # gone

  # patch is special
  patch: /usr/bin/patch
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            _BEFORE_NEXT,
            "binaries:\n  code: /a\n\n  # patch is special\n  patch: /usr/bin/patch\n",
        ),
        (
            _BEFORE_NEXT.replace("  code: /a\n", ""),
            "binaries:\n  # patch is special\n  patch: /usr/bin/patch\n",
        ),
    ],
    ids=["middle", "first"],
)
def test_config_remove_keeps_comment_of_next_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, expected: str
) -> None:
    local = tmp_path / "local.yaml"
    local.write_text(text, encoding="utf-8")
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local)
    monkeypatch.setattr("setforge.cli.config.LOCAL_CONFIG_PATH", local)

    result = CliRunner().invoke(
        app, ["config", "remove", "--local", "binaries.claude", "--yes"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert local.read_text(encoding="utf-8") == expected
