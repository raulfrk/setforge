"""Programmatic edits keep the config file's own indentation, EOL and BOM."""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest
from rich.console import Console
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import paths
from setforge.claude_yaml_editor import yaml_add_plugin_to_profile
from setforge.cli import app
from setforge.cli.cleanup import mark_orphan
from setforge.provision.protocol import Identity
from setforge.vscode_extensions import add_to_include, remove_from_include

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

    result = CliRunner().invoke(
        app, ["config", "remove", "--local", "binaries.claude", "--yes"]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert local.read_text(encoding="utf-8") == expected


# Every writer below renders through the same code as ``config add`` but had no
# check of its own: on a file whose lists are indented differently from one
# another it wrote the new entry glued onto the one above (``'keep.me -
# new.one'``) or YAML that does not parse, and exited 0.
_FLUSH_THEN_INDENTED = _MIXED
_INDENTED_THEN_FLUSH = _MIXED.replace("    - d\n", "      - d\n").replace(
    "      - keep.me\n", "    - keep.me\n"
)
_HAND_STYLED = [_FLUSH_THEN_INDENTED, _INDENTED_THEN_FLUSH]
_HAND_IDS = ["flush-then-indented", "indented-then-flush"]


def _base_profile(cfg: Path) -> dict[str, object]:
    loaded = YAML(typ="safe").load(cfg.read_text(encoding="utf-8"))
    profile = loaded["profiles"]["base"]
    assert isinstance(profile, dict)
    return profile


@pytest.mark.parametrize("text", _HAND_STYLED, ids=_HAND_IDS)
def test_ext_add_to_a_hand_styled_config_reads_back(tmp_path: Path, text: str) -> None:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(text, encoding="utf-8")

    assert add_to_include(cfg, "base", "new.one")

    assert _base_profile(cfg) == {
        "tracked_files": ["d"],
        "packages": ["keep.me", "new.one"],
    }
    after = cfg.read_text(encoding="utf-8")
    assert all(line.startswith("+") for line in _changed_lines(text, after))


@pytest.mark.parametrize("text", _HAND_STYLED, ids=_HAND_IDS)
def test_plugin_add_to_a_hand_styled_config_reads_back(
    tmp_path: Path, text: str
) -> None:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(text, encoding="utf-8")

    assert yaml_add_plugin_to_profile(cfg, "base", "lint@team")

    assert _base_profile(cfg) == {
        "tracked_files": ["d"],
        "packages": ["keep.me", "lint@team"],
    }
    after = cfg.read_text(encoding="utf-8")
    assert all(line.startswith("+") for line in _changed_lines(text, after))


@pytest.mark.parametrize(
    "text",
    [
        "plugins:\n  add:\n    - a\nprovision_ignore:\n- old\n",
        "plugins:\n  add:\n  - a\nprovision_ignore:\n    - old\n",
    ],
    ids=["indented-then-flush", "flush-then-indented"],
)
def test_cleanup_mark_orphan_in_a_hand_styled_local_yaml_reads_back(text: str) -> None:
    local = paths.local_config_path()
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(text, encoding="utf-8")

    mark_orphan(Identity(key="tool", display="tool"), console=Console())

    after = local.read_text(encoding="utf-8")
    loaded = YAML(typ="safe").load(after)
    assert loaded["plugins"] == {"add": ["a"]}
    assert loaded["provision_ignore"][0] == "old"
    assert len(loaded["provision_ignore"]) == 2
    assert all(line.startswith("+") for line in _changed_lines(text, after))


_LEGACY_PROFILE_FIELDS = """\
schema_version: "5.0"
minimum_version: "6.0"
tracked_files:
  d:
    src: x
    dst: y
packages:
  rg:
    type: cargo
    crate: rg
profiles:
  base:
    tracked_files:
{first}- d
  default:
    # the daily driver
    packages:
{second}- rg
    cargo_binaries: [fd]
"""


@pytest.mark.parametrize(
    ("first", "second"),
    [("    ", "      "), ("      ", "    ")],
    ids=["flush-then-indented", "indented-then-flush"],
)
def test_migrate_apply_on_a_hand_styled_config_reads_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str, second: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(
        _LEGACY_PROFILE_FIELDS.format(first=first, second=second), encoding="utf-8"
    )

    result = CliRunner().invoke(
        app, ["migrate", "--config", str(cfg), "--to", "6.0", "--apply", "--yes"]
    )

    assert result.exit_code == 0, result.output
    after = cfg.read_text(encoding="utf-8")
    loaded = YAML(typ="safe").load(after)
    assert loaded["profiles"]["base"] == {"tracked_files": ["d"]}
    assert loaded["profiles"]["default"] == {"packages": ["rg", "fd"]}
    assert "    # the daily driver\n" in after
    assert f"{first}- d\n" in after
    assert f"{second}- rg\n{second}- fd\n" in after


def test_ext_remove_below_a_blank_line_reads_back(tmp_path: Path) -> None:
    """Emptying a list that has a blank line under its key used to write
    ``packages:``, the blank line, then ``[]`` at the margin, which does not parse."""
    text = _BASE.replace("      - keep.me  # pinned\n", "\n      - keep.me\n")
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(text, encoding="utf-8")

    assert remove_from_include(cfg, "base", "keep.me")

    after = cfg.read_text(encoding="utf-8")
    assert _base_profile(cfg) == {"tracked_files": ["d"], "packages": []}
    assert "# Packages comment.\n" in after
