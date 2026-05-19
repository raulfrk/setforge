"""Tests for :mod:`setforge.migrations._yaml_ops`.

The load-bearing invariants under test:

- ``yaml_rt()`` returns a round-trip ``YAML`` with ``preserve_quotes=True``
  and a wide width so untouched lines are not reformatted.
- ``rename_key`` preserves comments above the key, end-of-line
  comments, AND insertion order (the renamed key occupies the same
  slot the old key did, not the trailing slot a naive
  ``pop()+assign`` produces).
- ``atomic_write_yaml`` round-trips through a sibling tmp file so a
  crash mid-write never leaves a half-rendered destination.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from setforge.migrations._yaml_ops import (
    atomic_write_yaml,
    rename_key,
    yaml_rt,
)


def _dump(data: object) -> str:
    """Serialize ``data`` through the project's round-trip YAML config."""
    buf = io.StringIO()
    yaml_rt().dump(data, buf)
    return buf.getvalue()


def test_yaml_rt_returns_round_trip_instance() -> None:
    yaml = yaml_rt()
    assert yaml.preserve_quotes is True
    assert yaml.width == 4096


def test_yaml_rt_round_trip_preserves_comments() -> None:
    source = (
        "# top-of-file comment\n"
        "version: 1\n"
        "tracked_files:\n"
        "  # comment above key\n"
        "  foo: bar  # eol comment\n"
    )
    data = yaml_rt().load(source)
    dumped = _dump(data)
    assert "# top-of-file comment" in dumped
    assert "# comment above key" in dumped
    assert "# eol comment" in dumped


def test_yaml_rt_preserves_double_quotes() -> None:
    source = 'name: "quoted value"\n'
    data = yaml_rt().load(source)
    dumped = _dump(data)
    assert '"quoted value"' in dumped


def test_rename_key_simple() -> None:
    source = "alpha: 1\nbeta: 2\ngamma: 3\n"
    data = yaml_rt().load(source)
    rename_key(data, "beta", "renamed_beta")
    assert list(data.keys()) == ["alpha", "renamed_beta", "gamma"]
    assert data["renamed_beta"] == 2


def test_rename_key_preserves_above_key_comment() -> None:
    source = (
        "alpha: 1\n"
        "# this comment sits above the key being renamed\n"
        "beta: 2\n"
        "gamma: 3\n"
    )
    data = yaml_rt().load(source)
    rename_key(data, "beta", "renamed_beta")
    dumped = _dump(data)
    assert "# this comment sits above the key being renamed" in dumped
    assert "renamed_beta: 2" in dumped
    # Ensure the comment is still attached to the renamed key — it must
    # appear BEFORE the renamed_beta line in the dumped output.
    above_line = dumped.index("# this comment sits above the key being renamed")
    key_line = dumped.index("renamed_beta:")
    assert above_line < key_line


def test_rename_key_preserves_end_of_line_comment() -> None:
    source = "alpha: 1\nbeta: 2  # important eol comment\ngamma: 3\n"
    data = yaml_rt().load(source)
    rename_key(data, "beta", "renamed_beta")
    dumped = _dump(data)
    assert "# important eol comment" in dumped
    # The eol comment must sit on the renamed key's line, not a
    # different line elsewhere in the doc.
    for line in dumped.splitlines():
        if "important eol comment" in line:
            assert line.lstrip().startswith("renamed_beta:")
            break
    else:  # pragma: no cover — guarded by the assertion above
        pytest.fail("eol comment did not survive rename")


def test_rename_key_preserves_insertion_order() -> None:
    source = "alpha: 1\nbeta: 2\ngamma: 3\ndelta: 4\n"
    data = yaml_rt().load(source)
    rename_key(data, "gamma", "renamed_gamma")
    assert list(data.keys()) == ["alpha", "beta", "renamed_gamma", "delta"]


def test_rename_key_no_op_when_old_equals_new() -> None:
    source = "alpha: 1\nbeta: 2\n"
    data = yaml_rt().load(source)
    rename_key(data, "alpha", "alpha")
    assert list(data.keys()) == ["alpha", "beta"]


def test_rename_key_raises_on_missing_key() -> None:
    source = "alpha: 1\nbeta: 2\n"
    data = yaml_rt().load(source)
    with pytest.raises(KeyError, match="not in node"):
        rename_key(data, "missing", "new")


def test_atomic_write_yaml_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    data = yaml_rt().load("key: value\n")
    atomic_write_yaml(target, data)
    assert target.read_text(encoding="utf-8") == "key: value\n"


def test_atomic_write_yaml_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    target.write_text("stale: content\n", encoding="utf-8")
    data = yaml_rt().load("fresh: content\n")
    atomic_write_yaml(target, data)
    assert target.read_text(encoding="utf-8") == "fresh: content\n"


def test_atomic_write_yaml_leaves_no_tmp_on_success(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    data = yaml_rt().load("k: v\n")
    atomic_write_yaml(target, data)
    tmp_residue = list(tmp_path.glob(".out.yaml.*.tmp"))
    assert tmp_residue == []


def test_atomic_write_yaml_round_trips_comments(tmp_path: Path) -> None:
    target = tmp_path / "out.yaml"
    source = (
        "# header\n"
        "version: 1\n"
        "# above\n"
        "alpha: 1  # eol\n"
    )
    data = yaml_rt().load(source)
    atomic_write_yaml(target, data)
    out = target.read_text(encoding="utf-8")
    assert "# header" in out
    assert "# above" in out
    assert "# eol" in out
