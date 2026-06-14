"""Regression tests for the `plugin add` / `plugin remove` YAML-binding bug.

Audit finding (Critical): ``plugin add`` declared the plugin in the top-level
``claude_plugins:`` registry under the BARE name but bound it to the profile
using the ``<name>@<marketplace>`` form. Every reader of
``profile.claude_plugins`` (``_validate_plugin_references``,
``_declared_plugin_ids``, ``sync_marketplace_cache``) treats entries as bare
registry keys, so the very next ``load_config`` raised ``ConfigError`` and the
config was bricked until hand-edited.

These tests drive the real Typer CLI (``--no-install`` so no claude binary is
needed) and then call ``load_config`` to prove the written config still loads.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.config import load_config

# A profile + a pre-declared marketplace so `yaml_add_marketplace` is a no-op
# (returns False) and `plugin add --no-install` never touches the claude binary.
_FIXTURE_YAML = """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
marketplaces:
  anthropics:
    source: github
    repo: anthropics/x
profiles:
  myprofile:
    tracked_files: [d]
"""


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_FIXTURE_YAML, encoding="utf-8")
    return cfg


def test_plugin_add_binds_bare_name_and_config_still_loads(tmp_path: Path) -> None:
    """`plugin add` writes a bare profile binding; the config reloads cleanly.

    On the old (`@`-form) behavior the profile would hold
    ``superpowers@anthropics`` while the registry key is ``superpowers``, and
    this ``load_config`` would raise ``ConfigError: ... reference undeclared
    plugin(s): myprofile.superpowers@anthropics``.
    """
    cfg = _write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "plugin",
            "add",
            "superpowers",
            "--from=github:anthropics/x",
            "-m",
            "anthropics",
            "--profile=myprofile",
            "--no-install",
            f"--config={cfg}",
        ],
    )
    assert result.exit_code == 0, result.output

    # The bricking symptom: load_config must NOT raise.
    reloaded = load_config(cfg)
    assert reloaded.profiles["myprofile"].claude_plugins == ["superpowers"]
    # The registry key matches the binding (bare name).
    assert "superpowers" in reloaded.claude_plugins


def test_plugin_add_at_form_argument_also_binds_bare_name(tmp_path: Path) -> None:
    """The ``<name>@<marketplace>`` argument form also stores a bare binding."""
    cfg = _write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "plugin",
            "add",
            "superpowers@anthropics",
            "--from=github:anthropics/x",
            "--profile=myprofile",
            "--no-install",
            f"--config={cfg}",
        ],
    )
    assert result.exit_code == 0, result.output

    reloaded = load_config(cfg)
    assert reloaded.profiles["myprofile"].claude_plugins == ["superpowers"]


def test_plugin_remove_at_form_drops_bare_binding(tmp_path: Path) -> None:
    """`plugin remove name@mp` strips the binding written by the corrected add.

    Removal must be symmetric with add: the profile holds the bare name, so a
    user passing the ``@``-form must still drop it.
    """
    cfg = _write_config(tmp_path)

    # Seed via the corrected add path.
    add = CliRunner().invoke(
        app,
        [
            "plugin",
            "add",
            "superpowers",
            "--from=github:anthropics/x",
            "-m",
            "anthropics",
            "--profile=myprofile",
            "--no-install",
            f"--config={cfg}",
        ],
    )
    assert add.exit_code == 0, add.output
    assert load_config(cfg).profiles["myprofile"].claude_plugins == ["superpowers"]

    # Remove using the @-form; without the strip the bare entry would survive.
    remove = CliRunner().invoke(
        app,
        [
            "plugin",
            "remove",
            "superpowers@anthropics",
            "--profile=myprofile",
            f"--config={cfg}",
        ],
    )
    assert remove.exit_code == 0, remove.output
    assert load_config(cfg).profiles["myprofile"].claude_plugins == []


def test_marketplace_add_rolls_back_yaml_on_binary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`marketplace add` must roll back the YAML entry when the claude binary
    call fails, leaving no orphaned marketplace declaration.

    On the old behavior the YAML was written before the binary call and never
    reverted, so a failing ``claude marketplace add`` left ``fresh-mp`` orphaned
    in the config repo. With the atomicity fix the file is byte-identical.
    """
    import setforge.cli.plugins as plugins_mod

    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_FIXTURE_YAML, encoding="utf-8")
    before = cfg.read_bytes()

    # claude appears available, but the registration call fails.
    monkeypatch.setattr(
        plugins_mod.claude_plugins_mod, "ensure_claude_available", lambda: None
    )

    def boom(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["claude"], stderr="nope")

    monkeypatch.setattr(plugins_mod.claude_plugins_mod, "marketplace_add", boom)

    result = CliRunner().invoke(
        app,
        ["marketplace", "add", "fresh-mp", "--from=github:o/r", f"--config={cfg}"],
    )
    assert result.exit_code == 1, result.output
    assert "error" in result.output.lower()
    # No orphaned entry, and the file is byte-identical to its pre-call state.
    assert cfg.read_bytes() == before, cfg.read_text(encoding="utf-8")
