"""CLI-level tests for the ``plugin`` / ``marketplace`` command groups.

Drives the real CLI via Typer's :class:`CliRunner`. Covers source-layer
resolution and clean error handling for failing ``claude`` subprocesses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app


@pytest.mark.parametrize(
    "argv",
    [
        ["plugin", "list", "--profile=x"],
        ["marketplace", "add", "mp", "--from=github:o/r"],
    ],
)
def test_plugin_marketplace_resolves_config(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """plugin/marketplace commands must call _resolve_config_arg before
    load_config so a configured source layer works outside the config-repo
    root."""
    import setforge.cli.plugins as plugins_mod

    seen: list[Path] = []

    def fake_resolve(config: Path) -> Path:
        seen.append(config)
        raise SystemExit(99)  # short-circuit before load_config

    monkeypatch.setattr(plugins_mod, "_resolve_config_arg", fake_resolve)
    CliRunner().invoke(app, argv)
    assert seen == [Path("setforge.yaml")]


@pytest.mark.parametrize(
    ("argv", "target_fn"),
    [
        (["marketplace", "add", "mp", "--from=github:o/r"], "marketplace_add"),
        (["marketplace", "remove", "mp"], "marketplace_remove"),
        (["marketplace", "update", "mp"], "marketplace_update"),
    ],
)
def test_marketplace_subprocess_error_is_clean(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], target_fn: str
) -> None:
    """A failing `claude` subprocess must surface as a clean error + exit 1,
    not a raw traceback."""
    import setforge.cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_resolve_config_arg", lambda c: c)
    monkeypatch.setattr(
        plugins_mod.claude_yaml_editor_mod,
        "yaml_add_marketplace",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        plugins_mod.claude_yaml_editor_mod,
        "yaml_remove_marketplace",
        lambda *a, **k: True,
    )

    def boom(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(1, ["claude"], stderr="nope")

    monkeypatch.setattr(plugins_mod.claude_plugins_mod, target_fn, boom)
    result = CliRunner().invoke(app, argv)
    # The command must CATCH the subprocess error and exit cleanly — not let
    # it escape (CliRunner stores an escaped exception in result.exception).
    assert result.exit_code == 1
    assert not isinstance(result.exception, subprocess.CalledProcessError)
    assert "error" in result.output.lower()


def test_plugin_remove_disable_subprocess_error_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing `claude plugin disable` during plugin remove --disable must
    surface as a clean error + exit 1, not a traceback."""
    import setforge.cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_resolve_config_arg", lambda c: c)
    monkeypatch.setattr(
        plugins_mod.claude_yaml_editor_mod,
        "yaml_remove_plugin_from_profile",
        lambda *a, **k: True,
    )

    def boom(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(1, ["claude"], stderr="nope")

    monkeypatch.setattr(plugins_mod.claude_plugins_mod, "plugin_disable", boom)
    result = CliRunner().invoke(
        app, ["plugin", "remove", "p@mp", "--disable", "--profile=x"]
    )
    assert result.exit_code == 1
    assert not isinstance(result.exception, subprocess.CalledProcessError)
    assert "error" in result.output.lower()


def test_plugin_add_marketplace_register_subprocess_error_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing `claude plugin marketplace add` during the plugin-add
    registration flow must surface as a clean error + exit 1, not a
    traceback (the catch arm inside _register_plugin_in_yaml)."""
    import setforge.cli.plugins as plugins_mod

    monkeypatch.setattr(plugins_mod, "_resolve_config_arg", lambda c: c)
    monkeypatch.setattr(plugins_mod, "load_config", lambda c: object())
    # New marketplace → the register path invokes `claude marketplace add`.
    monkeypatch.setattr(
        plugins_mod.claude_yaml_editor_mod, "yaml_add_marketplace", lambda *a, **k: True
    )

    def boom(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(1, ["claude"], stderr="nope")

    monkeypatch.setattr(plugins_mod.claude_plugins_mod, "marketplace_add", boom)
    result = CliRunner().invoke(
        app, ["plugin", "add", "p@mp", "--from=github:o/r", "--profile=x"]
    )
    assert result.exit_code == 1
    assert not isinstance(result.exception, subprocess.CalledProcessError)
    assert "error" in result.output.lower()
