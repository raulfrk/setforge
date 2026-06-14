"""Regression tests for `plugin`/`marketplace` add/remove edge-case bugs.

Covers three confirmed audit findings on ``setforge/cli/plugins.py``:

1. ``plugin add`` left an orphaned marketplace YAML entry when the claude
   binary call failed (the standalone ``marketplace add`` path rolled back,
   ``plugin add`` did not — asymmetric atomicity).
2. ``plugin remove --disable`` with a *bare* plugin name passed that bare id
   straight to ``claude plugin disable``, which the binary rejects; the id
   must be reconstructed to ``<name>@<marketplace>`` from the registry.
3. Under ``claude.install_mode: local-clone`` the explicit CLI add paths
   handed claude the raw GitHub slug instead of routing through
   ``resolve_marketplace_source`` (the cache PATH the reconcile path uses),
   defeating the offline cache and breaking reconcile idempotency.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import setforge.cli.plugins as plugins_mod
from setforge.binaries import ClaudeLocalConfig, HostLocalConfig
from setforge.cli import app
from setforge.config import (
    ClaudeInstallMode,
    MarketplaceSource,
    MarketplaceSourceKind,
)

_FIXTURE_YAML = """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
profiles:
  myprofile:
    tracked_files: [d]
"""

# Variant with a pre-existing marketplace so a rollback of the just-added entry
# restores byte-identity (the empty `marketplaces:` mapping is never created
# from scratch). Mirrors test_marketplace_add_rolls_back_yaml_on_binary_failure.
_FIXTURE_YAML_WITH_MP = """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
marketplaces:
  existing:
    source: github
    repo: a/b
profiles:
  myprofile:
    tracked_files: [d]
"""

# A profile that already declares plugin `superpowers` under marketplace `mp`,
# for the bare-name disable test.
_FIXTURE_YAML_WITH_PLUGIN = """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
marketplaces:
  mp:
    source: github
    repo: o/r
claude_plugins:
  superpowers:
    marketplace: mp
profiles:
  myprofile:
    tracked_files: [d]
    claude_plugins: [superpowers]
"""


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _regular_host_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force install_mode REGULAR so _resolve_add_source is a passthrough."""
    monkeypatch.setattr(
        plugins_mod.binaries,
        "load_host_local_config",
        lambda: HostLocalConfig(
            claude=ClaudeLocalConfig(install_mode=ClaudeInstallMode.REGULAR)
        ),
    )


# ---------------------------------------------------------------------------
# Finding 1: plugin add rolls back the orphaned marketplace YAML entry
# ---------------------------------------------------------------------------
def test_plugin_add_rolls_back_marketplace_on_binary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plugin add` to a fresh marketplace must roll the YAML entry back when
    the ``claude marketplace add`` binary call fails — byte-identical config."""
    cfg = _write(tmp_path, _FIXTURE_YAML_WITH_MP)
    before = cfg.read_bytes()
    _regular_host_local(monkeypatch)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["claude"], stderr="nope")

    monkeypatch.setattr(plugins_mod.claude_plugins_mod, "marketplace_add", boom)

    res = CliRunner().invoke(
        app,
        [
            "plugin",
            "add",
            "superpowers@fresh-mp",
            "--from=github:o/r",
            "--profile=myprofile",
            f"--config={cfg}",
        ],
    )
    assert res.exit_code == 1, res.output
    assert "error" in res.output.lower()
    # No orphaned `fresh-mp` declaration: the file is byte-identical.
    assert cfg.read_bytes() == before, cfg.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Finding 2: plugin remove --disable reconstructs <name>@<marketplace>
# ---------------------------------------------------------------------------
def test_plugin_remove_disable_bare_name_reconstructs_full_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plugin remove superpowers --disable` must call plugin_disable with the
    full ``superpowers@mp`` id, not the bare name claude would reject."""
    cfg = _write(tmp_path, _FIXTURE_YAML_WITH_PLUGIN)

    captured: list[str] = []

    def record(plugin_id: str) -> None:
        captured.append(plugin_id)

    monkeypatch.setattr(plugins_mod.claude_plugins_mod, "plugin_disable", record)

    res = CliRunner().invoke(
        app,
        [
            "plugin",
            "remove",
            "superpowers",
            "--disable",
            "--profile=myprofile",
            f"--config={cfg}",
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured == ["superpowers@mp"], captured


def test_plugin_remove_disable_at_form_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``@``-form id is passed straight through unchanged."""
    cfg = _write(tmp_path, _FIXTURE_YAML_WITH_PLUGIN)

    captured: list[str] = []
    monkeypatch.setattr(
        plugins_mod.claude_plugins_mod, "plugin_disable", captured.append
    )

    res = CliRunner().invoke(
        app,
        [
            "plugin",
            "remove",
            "superpowers@mp",
            "--disable",
            "--profile=myprofile",
            f"--config={cfg}",
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured == ["superpowers@mp"], captured


def test_plugin_remove_disable_unknown_bare_name_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare name absent from the registry exits 1 instead of disabling it."""
    cfg = _write(tmp_path, _FIXTURE_YAML_WITH_PLUGIN)

    called: list[str] = []
    monkeypatch.setattr(plugins_mod.claude_plugins_mod, "plugin_disable", called.append)

    res = CliRunner().invoke(
        app,
        [
            "plugin",
            "remove",
            "ghost",
            "--disable",
            "--profile=myprofile",
            f"--config={cfg}",
        ],
    )
    assert res.exit_code == 1, res.output
    assert called == [], called
    assert "ghost" in res.output


# ---------------------------------------------------------------------------
# Finding 3: local-clone routes the CLI add source through the cache PATH
# ---------------------------------------------------------------------------
def test_marketplace_add_local_clone_routes_source_through_cache_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under local-clone, `marketplace add` must hand claude a PATH-kind source
    (the on-disk cache), not the raw GitHub slug."""
    cfg = _write(tmp_path, _FIXTURE_YAML)
    cache_dir = tmp_path / "cache" / "r"

    monkeypatch.setattr(
        plugins_mod.binaries,
        "load_host_local_config",
        lambda: HostLocalConfig(
            claude=ClaudeLocalConfig(install_mode=ClaudeInstallMode.LOCAL_CLONE)
        ),
    )
    monkeypatch.setattr(
        plugins_mod.claude_plugins_mod, "ensure_claude_available", lambda: None
    )

    def fake_resolve(
        source: MarketplaceSource,
        mode: ClaudeInstallMode,
        *,
        cache_root: Path | None = None,
        mp_name: str | None = None,
        auto: bool = False,
    ) -> MarketplaceSource:
        assert mode is ClaudeInstallMode.LOCAL_CLONE
        return MarketplaceSource(source=MarketplaceSourceKind.PATH, path=cache_dir)

    monkeypatch.setattr(
        plugins_mod.claude_mp_cache_mod, "resolve_marketplace_source", fake_resolve
    )

    seen: list[MarketplaceSource] = []

    def record_add(name: str, source: MarketplaceSource) -> None:
        seen.append(source)

    monkeypatch.setattr(plugins_mod.claude_plugins_mod, "marketplace_add", record_add)

    res = CliRunner().invoke(
        app,
        ["marketplace", "add", "mp", "--from=github:o/r", f"--config={cfg}"],
    )
    assert res.exit_code == 0, res.output
    assert len(seen) == 1, seen
    assert seen[0].source is MarketplaceSourceKind.PATH
    assert seen[0].path == cache_dir


def test_marketplace_add_cache_miss_rolls_back_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A MarketplaceCacheMiss while resolving the source must roll back the YAML
    entry (atomicity holds for the local-clone failure path too)."""
    cfg = _write(tmp_path, _FIXTURE_YAML_WITH_MP)
    before = cfg.read_bytes()

    monkeypatch.setattr(
        plugins_mod.binaries,
        "load_host_local_config",
        lambda: HostLocalConfig(
            claude=ClaudeLocalConfig(install_mode=ClaudeInstallMode.LOCAL_CLONE)
        ),
    )
    monkeypatch.setattr(
        plugins_mod.claude_plugins_mod, "ensure_claude_available", lambda: None
    )

    from setforge.errors import MarketplaceCacheMiss

    def boom(*_args: object, **_kwargs: object) -> MarketplaceSource:
        raise MarketplaceCacheMiss("cache absent")

    monkeypatch.setattr(
        plugins_mod.claude_mp_cache_mod, "resolve_marketplace_source", boom
    )

    res = CliRunner().invoke(
        app,
        ["marketplace", "add", "mp", "--from=github:o/r", f"--config={cfg}"],
    )
    assert res.exit_code == 1, res.output
    assert cfg.read_bytes() == before, cfg.read_text(encoding="utf-8")
