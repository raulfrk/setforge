"""Regression tests for audit finding `extinherit`.

`add_to_include` must refuse an extension that is excluded by an ancestor
profile via the `extends:` chain — not only the profile's own literal
`exclude`. Merged exclude "always wins" on reconcile, so without this guard
the addition lands in the child's `include` yet is silently dropped on every
reconcile (effective install set = include - resolved_exclude == empty).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.config import load_config
from setforge.errors import ConfigError
from setforge.vscode_extensions import add_to_include

_PARENT_EXCLUDE_FIXTURE = """\
version: 1
tracked_files:
  d: {src: x, dst: y}
profiles:
  parent:
    tracked_files: [d]
    extensions:
      exclude:
        - vendor.ext
  child:
    extends: parent
    tracked_files: [d]
"""

_GRANDPARENT_EXCLUDE_FIXTURE = """\
version: 1
tracked_files:
  d: {src: x, dst: y}
profiles:
  grandparent:
    tracked_files: [d]
    extensions:
      exclude:
        - vendor.ext
  parent:
    extends: grandparent
    tracked_files: [d]
  child:
    extends: parent
    tracked_files: [d]
"""


def _write(tmp_path: Path, fixture: str) -> Path:
    p = tmp_path / "setforge.yaml"
    p.write_text(fixture, encoding="utf-8")
    return p


def test_add_to_include_rejects_parent_excluded(tmp_path: Path) -> None:
    """Adding an extension excluded by the direct parent raises ConfigError
    naming the declaring profile and the exclude mechanism."""
    p = _write(tmp_path, _PARENT_EXCLUDE_FIXTURE)
    with pytest.raises(ConfigError, match="parent"):
        add_to_include(p, "child", "vendor.ext")
    # And the file is not mutated — the addition was refused, not written.
    cfg = load_config(p)
    assert "vendor.ext" not in cfg.profiles["child"].extensions.include


def test_add_to_include_reject_message_mentions_exclude(tmp_path: Path) -> None:
    p = _write(tmp_path, _PARENT_EXCLUDE_FIXTURE)
    with pytest.raises(ConfigError, match="exclude"):
        add_to_include(p, "child", "vendor.ext")


def test_add_to_include_rejects_grandparent_excluded(tmp_path: Path) -> None:
    """The guard walks the full extends: chain, not just the direct parent."""
    p = _write(tmp_path, _GRANDPARENT_EXCLUDE_FIXTURE)
    with pytest.raises(ConfigError, match="grandparent"):
        add_to_include(p, "child", "vendor.ext")


def test_add_to_include_allows_unexcluded_in_child(tmp_path: Path) -> None:
    """An extension NOT excluded anywhere in the chain still adds normally."""
    p = _write(tmp_path, _PARENT_EXCLUDE_FIXTURE)
    added = add_to_include(p, "child", "fine.ext")
    assert added is True
    cfg = load_config(p)
    assert "fine.ext" in cfg.profiles["child"].extensions.include
