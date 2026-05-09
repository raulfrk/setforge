"""Tests for ``my-setup validate`` subcommand.

Covers each of the six failure modes plus a clean-run baseline:
1. Clean run → exit 0.
2. Pydantic schema error → exit 1, message names the key.
3. Missing profile (--profile=does-not-exist) → exit 1.
4. Profile cycle (a extends b, b extends a) → exit 1.
5. Missing tracked src → exit 1.
6. Unrenderable Jinja2 template → exit 1.
7. claude_plugins references unknown marketplace → exit 1.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from my_setup.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Minimal YAML builder helpers
# ---------------------------------------------------------------------------

_CLEAN_YAML = """\
version: 1
dotfiles:
  d:
    src: tracked_file.txt
    dst: ~/.some-dotfile
profiles:
  p:
    dotfiles: [d]
"""

_CLEAN_WITH_PLUGIN_YAML = """\
version: 1
dotfiles:
  d:
    src: tracked_file.txt
    dst: ~/.some-dotfile
marketplaces:
  my-market:
    source: github
    repo: owner/repo
claude_plugins:
  myplugin:
    marketplace: my-market
profiles:
  p:
    dotfiles: [d]
    claude_plugins: [myplugin]
"""


def _write_config(tmp_path: Path, content: str, *, create_src: bool = True) -> Path:
    """Write my_setup.yaml and optionally create the dummy tracked file."""
    cfg = tmp_path / "my_setup.yaml"
    cfg.write_text(content, encoding="utf-8")
    (tmp_path / "tracked").mkdir(exist_ok=True)
    if create_src:
        (tmp_path / "tracked" / "tracked_file.txt").write_text("data\n", encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# Test 1: clean run exits 0
# ---------------------------------------------------------------------------


def test_validate_clean_run_exits_0(tmp_path: Path) -> None:
    """A well-formed config with all srcs present exits 0 and prints 'ok'."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = runner.invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_validate_all_clean_exits_0(tmp_path: Path) -> None:
    """--all on a well-formed config exits 0."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = runner.invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


# ---------------------------------------------------------------------------
# Test 2: Pydantic schema error
# ---------------------------------------------------------------------------


def test_validate_schema_error_exits_1(tmp_path: Path) -> None:
    """Pydantic schema error (extra field on dotfile) → exit 1."""
    bad_yaml = """\
version: 1
dotfiles:
  d:
    src: tracked_file.txt
    dst: ~/.some-dotfile
    not_a_real_field: true
profiles:
  p:
    dotfiles: [d]
"""
    cfg = _write_config(tmp_path, bad_yaml)
    result = runner.invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # Message should mention the bad key
    assert "not_a_real_field" in result.output or "schema" in result.output


# ---------------------------------------------------------------------------
# Test 3: missing profile
# ---------------------------------------------------------------------------


def test_validate_missing_profile_exits_1(tmp_path: Path) -> None:
    """--profile= pointing at a non-existent profile → exit 1."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = runner.invoke(
        app, ["validate", "--profile=does-not-exist", f"--config={cfg}"]
    )
    assert result.exit_code == 1, result.output
    combined = result.output
    assert "does-not-exist" in combined or "not found" in combined


# ---------------------------------------------------------------------------
# Test 4: profile cycle
# ---------------------------------------------------------------------------


def test_validate_profile_cycle_exits_1(tmp_path: Path) -> None:
    """Profile cycle (a extends b, b extends a) → exit 1."""
    cyclic_yaml = """\
version: 1
dotfiles:
  d:
    src: tracked_file.txt
    dst: ~/.some-dotfile
profiles:
  a:
    extends: b
    dotfiles: [d]
  b:
    extends: a
    dotfiles: [d]
"""
    cfg = _write_config(tmp_path, cyclic_yaml)
    result = runner.invoke(app, ["validate", "--profile=a", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "cycle" in result.output or "profile" in result.output


# ---------------------------------------------------------------------------
# Test 5: missing tracked src
# ---------------------------------------------------------------------------


def test_validate_missing_src_exits_1(tmp_path: Path) -> None:
    """A dotfile whose src does not exist on disk → exit 1."""
    # create_src=False so tracked_file.txt is absent
    cfg = _write_config(tmp_path, _CLEAN_YAML, create_src=False)
    result = runner.invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    combined = result.output
    assert "tracked_file.txt" in combined or "does not exist" in combined


# ---------------------------------------------------------------------------
# Test 6: unrenderable Jinja2 template
# ---------------------------------------------------------------------------


def test_validate_unrenderable_template_exits_1(tmp_path: Path) -> None:
    """A Jinja2 syntax error in a template dst → exit 1."""
    broken_template_yaml = """\
version: 1
dotfiles:
  d:
    src: tracked_file.txt
    dst: "{% for x in %}broken"
    template: true
profiles:
  p:
    dotfiles: [d]
"""
    cfg = _write_config(tmp_path, broken_template_yaml)
    result = runner.invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    assert "template" in result.output or "unrenderable" in result.output


# ---------------------------------------------------------------------------
# Test 7: claude_plugins references unknown marketplace
# ---------------------------------------------------------------------------


def test_validate_unknown_marketplace_exits_1(tmp_path: Path) -> None:
    """A plugin whose marketplace is absent from the marketplaces block → exit 1."""
    bad_mp_yaml = """\
version: 1
dotfiles:
  d:
    src: tracked_file.txt
    dst: ~/.some-dotfile
marketplaces: {}
claude_plugins:
  myplugin:
    marketplace: ghost-market
profiles:
  p:
    dotfiles: [d]
    claude_plugins: [myplugin]
"""
    cfg = _write_config(tmp_path, bad_mp_yaml)
    result = runner.invoke(app, ["validate", "--profile=p", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    combined = result.output
    assert "ghost-market" in combined or "marketplace" in combined


# ---------------------------------------------------------------------------
# Mutex flag validation
# ---------------------------------------------------------------------------


def test_validate_both_flags_exits_2(tmp_path: Path) -> None:
    """Passing both --profile and --all exits 2."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = runner.invoke(
        app, ["validate", "--profile=p", "--all", f"--config={cfg}"]
    )
    assert result.exit_code == 2, result.output


def test_validate_neither_flag_exits_2(tmp_path: Path) -> None:
    """Passing neither --profile nor --all exits 2."""
    cfg = _write_config(tmp_path, _CLEAN_YAML)
    result = runner.invoke(app, ["validate", f"--config={cfg}"])
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Aggregation: multiple failures reported together
# ---------------------------------------------------------------------------


def test_validate_aggregates_failures(tmp_path: Path) -> None:
    """Two profiles each with a missing src should both appear in output."""
    two_profile_yaml = """\
version: 1
dotfiles:
  d1:
    src: missing1.txt
    dst: ~/.d1
  d2:
    src: missing2.txt
    dst: ~/.d2
profiles:
  pa:
    dotfiles: [d1]
  pb:
    dotfiles: [d2]
"""
    cfg = _write_config(tmp_path, two_profile_yaml, create_src=False)
    result = runner.invoke(app, ["validate", "--all", f"--config={cfg}"])
    assert result.exit_code == 1, result.output
    # Both missing srcs should be reported
    assert "missing1.txt" in result.output
    assert "missing2.txt" in result.output
