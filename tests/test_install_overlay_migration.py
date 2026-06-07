"""Install-loop integration: transparent local.yaml overlay-span rewrite.

Drives the real ``setforge install`` against a sandboxed config repo +
``local.yaml`` carrying a legacy ``host_local_sections`` block, and asserts:

- install rewrites ``local.yaml`` in place (legacy block → ``spans`` overlay),
- the rewrite is recorded in the install transition so ``revert`` restores the
  pre-migration ``local.yaml`` byte-for-byte AND preserves its file mode,
- a second install is a no-op on ``local.yaml`` (idempotent).

The ``local.yaml`` path is the conftest-redirected ``setforge.source``
constant (``tmp_path / "local.yaml"``), so the migration never touches the
dev-host real config.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app

_PROFILE = "test-overlay-mig"

_DOC = """\
# Title

## Notes

upstream notes body
"""

_LOCAL_YAML = """\
# host config
tracked_files:
  doc:
    host_local_sections:
      my-notes:
        anchor:
          kind: after-heading
          value: "Notes"
        body: |
          host-local notes
"""


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_omig/doc.md\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path) -> None:
    src = repo / "tracked" / "doc.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(_DOC, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _local_yaml_path(tmp_path: Path) -> Path:
    # conftest._isolated_local_config redirects source.LOCAL_CONFIG_PATH here.
    return tmp_path / "local.yaml"


def _install(config: Path, *, no_transition: bool = False) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    if no_transition:
        args.append("--no-transition")
    return CliRunner().invoke(app, args)


def test_install_rewrites_local_yaml_host_local_sections(
    repo: Path, tmp_path: Path
) -> None:
    """Install retires the legacy host_local_sections block into spans overlay."""
    _write_tracked(repo)
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    local.write_text(_LOCAL_YAML, encoding="utf-8")

    result = _install(config, no_transition=True)

    assert result.exit_code == 0, result.output
    text = local.read_text(encoding="utf-8")
    assert "host_local_sections:" not in text
    assert "kind: overlay" in text
    assert "# host config" in text  # comment preserved
    # The body never leaked into the tracked source.
    tracked = (repo / "tracked" / "doc.md").read_text(encoding="utf-8")
    assert "host-local notes" not in tracked


def test_install_is_idempotent_on_local_yaml(repo: Path, tmp_path: Path) -> None:
    """A second install leaves the already-migrated local.yaml untouched."""
    _write_tracked(repo)
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    local.write_text(_LOCAL_YAML, encoding="utf-8")

    assert _install(config, no_transition=True).exit_code == 0
    after_first = local.read_bytes()
    assert _install(config, no_transition=True).exit_code == 0
    assert local.read_bytes() == after_first


def test_revert_restores_local_yaml_bytes_and_mode(repo: Path, tmp_path: Path) -> None:
    """Revert restores the pre-migration local.yaml byte-for-byte + st_mode."""
    _write_tracked(repo)
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    local.write_text(_LOCAL_YAML, encoding="utf-8")
    local.chmod(0o640)
    pre_bytes = local.read_bytes()
    pre_mode = stat.S_IMODE(local.stat().st_mode)

    install = _install(config)
    assert install.exit_code == 0, install.output
    # Sanity: the install actually rewrote local.yaml.
    assert local.read_bytes() != pre_bytes

    revert = CliRunner().invoke(
        app,
        ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"],
    )
    assert revert.exit_code == 0, revert.output
    assert local.read_bytes() == pre_bytes
    assert stat.S_IMODE(local.stat().st_mode) == pre_mode
