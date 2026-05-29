"""CLI-level tests for the ``plugin`` / ``marketplace`` command groups.

Drives the real CLI via Typer's :class:`CliRunner`. Covers source-layer
resolution and clean error handling for failing ``claude`` subprocesses.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import claude_plugins as claude_plugins_mod
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


def test_marketplace_update_does_not_resolve_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`marketplace update` only shells to `claude` and never loads the
    config, so it must NOT call _resolve_config_arg — resolving would add a
    spurious NoSourceConfigured failure mode for a command needing no
    source. Regression guard for the resolve being re-added."""
    import setforge.cli.plugins as plugins_mod

    resolved: list[Path] = []

    def fake_resolve(config: Path) -> Path:
        resolved.append(config)
        raise SystemExit(99)  # would short-circuit before the claude call

    called: list[str] = []
    monkeypatch.setattr(plugins_mod, "_resolve_config_arg", fake_resolve)
    monkeypatch.setattr(
        plugins_mod.claude_plugins_mod,
        "marketplace_update",
        lambda name: called.append(name),
    )
    result = CliRunner().invoke(app, ["marketplace", "update", "mp"])
    assert result.exit_code == 0, result.output
    assert resolved == []  # the source layer was never consulted
    assert called == ["mp"]  # the command reached the claude call


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


# ---------------------------------------------------------------------------
# marketplace add/update/remove on a missing claude CLI: non-zero exit +
# atomic refusal (YAML byte-identical before/after on add/remove).
# ---------------------------------------------------------------------------

_MARKETPLACE_FIXTURE_YAML = """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
marketplaces:
  existing:
    source: github
    repo: owner/repo
profiles:
  p:
    tracked_files: [d]
"""


@pytest.fixture(autouse=True)
def _clear_claude_bin_cache() -> Iterator[None]:
    """Reset the module-global ``_get_claude_bin`` cache around every test.

    ``_get_claude_bin`` is ``functools.lru_cache(maxsize=1)`` and shared
    across the process, so a resolved-or-missing verdict from one case
    leaks into later cases unless cleared. Clearing both before and after
    keeps marketplace-availability cases order-independent.
    """
    claude_plugins_mod._get_claude_bin.cache_clear()
    yield
    claude_plugins_mod._get_claude_bin.cache_clear()


def _write_marketplace_config(tmp_path: Path) -> Path:
    """Write a setforge.yaml with one declared marketplace under tmp_path."""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_MARKETPLACE_FIXTURE_YAML, encoding="utf-8")
    return cfg


def test_marketplace_add_missing_claude_exits_nonzero_and_leaves_yaml_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`marketplace add` with claude absent exits non-zero and writes no YAML."""
    cfg = _write_marketplace_config(tmp_path)
    before = cfg.read_bytes()

    monkeypatch.setattr("setforge.claude_plugins.resolve_binary", lambda _: None)

    result = CliRunner().invoke(
        app, ["marketplace", "add", "fresh", "--from=github:o/r", f"--config={cfg}"]
    )
    assert result.exit_code != 0, result.output
    assert "error" in result.output.lower()
    assert cfg.read_bytes() == before


def test_marketplace_remove_missing_claude_exits_nonzero_and_leaves_yaml_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`marketplace remove` with claude absent exits non-zero and writes no YAML.

    The fixture declares a marketplace that ``remove`` would otherwise
    delete, so a byte-identical file proves the YAML editor never ran.
    """
    cfg = _write_marketplace_config(tmp_path)
    before = cfg.read_bytes()

    monkeypatch.setattr("setforge.claude_plugins.resolve_binary", lambda _: None)

    result = CliRunner().invoke(
        app, ["marketplace", "remove", "existing", f"--config={cfg}"]
    )
    assert result.exit_code != 0, result.output
    assert "error" in result.output.lower()
    assert cfg.read_bytes() == before


def test_marketplace_update_missing_claude_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`marketplace update` with claude absent exits non-zero (no warning swallow)."""
    monkeypatch.setattr("setforge.claude_plugins.resolve_binary", lambda _: None)

    result = CliRunner().invoke(app, ["marketplace", "update", "existing"])
    assert result.exit_code != 0, result.output
    assert "error" in result.output.lower()


def test_marketplace_add_with_claude_present_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`marketplace add` with claude present registers the marketplace (exit 0)."""
    cfg = _write_marketplace_config(tmp_path)

    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary", lambda _: Path("/usr/bin/claude")
    )
    added: list[str] = []
    monkeypatch.setattr(
        claude_plugins_mod, "marketplace_add", lambda name, source: added.append(name)
    )

    result = CliRunner().invoke(
        app, ["marketplace", "add", "fresh", "--from=github:o/r", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    assert added == ["fresh"]
    assert "registered marketplace: fresh" in result.output


def test_marketplace_remove_with_claude_present_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`marketplace remove` with claude present removes the marketplace (exit 0)."""
    cfg = _write_marketplace_config(tmp_path)

    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary", lambda _: Path("/usr/bin/claude")
    )
    removed: list[str] = []
    monkeypatch.setattr(
        claude_plugins_mod, "marketplace_remove", lambda name: removed.append(name)
    )

    result = CliRunner().invoke(
        app, ["marketplace", "remove", "existing", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    assert removed == ["existing"]
    assert "removed marketplace: existing" in result.output


def test_marketplace_update_with_claude_present_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`marketplace update` with claude present updates the marketplace (exit 0)."""
    monkeypatch.setattr(
        "setforge.claude_plugins.resolve_binary", lambda _: Path("/usr/bin/claude")
    )
    updated: list[str] = []
    monkeypatch.setattr(
        claude_plugins_mod, "marketplace_update", lambda name: updated.append(name)
    )

    result = CliRunner().invoke(app, ["marketplace", "update", "existing"])
    assert result.exit_code == 0, result.output
    assert updated == ["existing"]
    assert "updated marketplace: existing" in result.output
