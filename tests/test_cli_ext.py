"""CLI-level tests for the ``ext`` command group.

Drives the real CLI via Typer's :class:`CliRunner`. Covers source-layer
resolution (every command must consult the source layer before
``load_config``) and clean error handling for extension install failures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app


def test_ext_list_resolves_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """ext list must call _resolve_config_arg before load_config so a
    configured source layer works outside the config-repo root."""
    import setforge.cli.ext as ext_mod

    seen: list[Path] = []

    def fake_resolve(config: Path) -> Path:
        seen.append(config)
        raise SystemExit(99)  # short-circuit before load_config

    monkeypatch.setattr(ext_mod, "_resolve_config_arg", fake_resolve)
    CliRunner().invoke(app, ["ext", "list", "--profile=x"])
    assert seen == [Path("setforge.yaml")]


def test_ext_add_handles_install_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero `code --install-extension` raises ExtensionInstallFailed,
    which must surface as a clean error + exit 1, not a traceback."""
    import setforge.cli.ext as ext_mod
    from setforge.errors import ExtensionInstallFailed

    monkeypatch.setattr(ext_mod, "_resolve_config_arg", lambda c: c)
    monkeypatch.setattr(
        ext_mod.vscode_extensions, "add_to_include", lambda *a, **k: True
    )

    def boom(_ext_id: str) -> None:
        raise ExtensionInstallFailed("code --install-extension exited 1")

    monkeypatch.setattr(ext_mod.vscode_extensions, "install_one", boom)
    result = CliRunner().invoke(app, ["ext", "add", "pub.ext", "--profile=x"])
    assert result.exit_code == 1
    assert "code --install-extension exited 1" in result.output
    assert "Traceback" not in result.output


# ---- --name override + collision guard (real-config harness) -------------

_ADD_FIXTURE = """\
version: 1
schema_version: "6.0"

tracked_files:
  d:
    src: x
    dst: y

packages:
  keep.me:
    type: extension
    extension: keep.me

profiles:
  base:
    tracked_files:
      - d
    packages:
      - keep.me
"""


def _write_add_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "setforge.yaml"
    p.write_text(_ADD_FIXTURE, encoding="utf-8")
    return p


def _invoke_add(cfg: Path, args: list[str]):
    """Run ``ext add`` against a real config with install disabled."""
    runner = CliRunner()
    # Point the config at the real fixture; skip the code CLI install.
    return runner.invoke(
        app,
        ["ext", "add", *args, "--no-install", "--profile=base", f"--config={cfg}"],
    )


def test_ext_add_name_override_mints_under_new_key(tmp_path: Path) -> None:
    """`ext add <id> --name <key>` mints packages:{<key>:{type:extension,
    extension:<id>}} and appends <key> to the profile packages ref list."""
    from setforge.config import ExtensionPackage, load_config

    cfg = _write_add_fixture(tmp_path)
    result = _invoke_add(cfg, ["ms-python.python", "--name", "py"])
    assert result.exit_code == 0, result.output

    loaded = load_config(cfg)
    assert loaded.schema_version == "6.0"
    entry = loaded.packages["py"]
    assert isinstance(entry, ExtensionPackage)
    assert entry.extension == "ms-python.python"
    assert "py" in loaded.profiles["base"].packages


def test_ext_add_colliding_key_raises(tmp_path: Path) -> None:
    """`ext add K` where K already maps to a different-body package raises
    ConfigError naming K + mentioning --name; the file is unchanged."""
    fixture = """\
version: 1
schema_version: "6.0"

tracked_files:
  d:
    src: x
    dst: y

packages:
  ripgrep:
    type: cargo
    crate: ripgrep

profiles:
  base:
    tracked_files:
      - d
    packages:
      - ripgrep
"""
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(fixture, encoding="utf-8")
    before = cfg.read_text()

    result = _invoke_add(cfg, ["ripgrep"])
    assert result.exit_code != 0
    # The ConfigError propagates (main() renders it as `error: ...` + exit 1).
    from setforge.errors import ConfigError

    assert isinstance(result.exception, ConfigError)
    message = str(result.exception)
    assert "ripgrep" in message
    assert "--name" in message
    # Nothing was changed.
    assert cfg.read_text() == before


def test_ext_add_identical_body_is_idempotent(tmp_path: Path) -> None:
    """Re-adding an id already mapped to the identical extension body reuses
    the entry — no duplicate, no error."""
    from setforge.config import load_config

    cfg = _write_add_fixture(tmp_path)
    result = _invoke_add(cfg, ["keep.me"])
    assert result.exit_code == 0, result.output

    loaded = load_config(cfg)
    assert loaded.profiles["base"].packages.count("keep.me") == 1
    text = cfg.read_text()
    # No duplicate top-level entry.
    assert text.count("extension: keep.me") == 1
