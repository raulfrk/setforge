"""CLI-level tests for the ``ext`` command group.

Drives the real CLI via Typer's :class:`CliRunner`. Covers source-layer
resolution (every command must consult the source layer before
``load_config``) and clean error handling for extension install failures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from setforge.cli import app
from setforge.config import ExtensionPackage, load_config


def test_ext_list_resolves_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """ext list must call _resolve_config_arg before load_config so a
    configured source layer works outside the config-repo root."""
    import setforge.cli.ext as ext_mod

    seen: list[Path | None] = []

    def fake_resolve(config: Path | None) -> Path:
        seen.append(config)
        raise SystemExit(99)  # short-circuit before load_config

    monkeypatch.setattr(ext_mod, "_resolve_config_arg", fake_resolve)
    CliRunner().invoke(app, ["ext", "list", "--profile=x"])
    assert seen == [None]


def test_ext_add_handles_install_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero `code --install-extension` raises ExtensionInstallFailed,
    which must surface as a clean error + exit 1, not a traceback."""
    import setforge.cli.ext as ext_mod
    from setforge.errors import ExtensionInstallFailed

    monkeypatch.setattr(
        ext_mod, "_resolve_config_arg", lambda c: c or Path("setforge.yaml")
    )
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


# ---- --name flag threads through the CLI (real-config harness) -----------
# The mint/collision/idempotency LOGIC is unit-tested directly in
# tests/test_vscode_extensions.py; this tier only covers the CLI wiring.

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


def _invoke_add(cfg: Path, args: list[str]) -> Result:
    """Run ``ext add`` against a real config with install disabled."""
    runner = CliRunner()
    # Point the config at the real fixture; skip the code CLI install.
    return runner.invoke(
        app,
        ["ext", "add", *args, "--no-install", "--profile=base", f"--config={cfg}"],
    )


def test_ext_add_name_flag_threads_through_and_echoes_key(tmp_path: Path) -> None:
    """CLI-layer concern the unit tests can't cover: ``--name`` reaches the
    mint (key≠id lands in the config) AND the stdout echo substitutes the
    KEY for the interesting key≠id case."""
    cfg = _write_add_fixture(tmp_path)
    result = _invoke_add(cfg, ["ms-python.python", "--name", "py"])
    assert result.exit_code == 0, result.output

    # Flag threaded through to the mint: key≠id landed in the config.
    loaded = load_config(cfg)
    entry = loaded.packages["py"]
    assert isinstance(entry, ExtensionPackage)
    assert entry.extension == "ms-python.python"

    # Echo substitutes the KEY (not the id) in the key≠id case.
    assert "added to base.packages: py (extension ms-python.python)" in result.output
