"""Regression tests: init must not silently clobber a customized local.yaml.

Audit finding ``init_clobber_localyaml``: ``setforge init --config-repo``
(and bare ``init --no-prompt``) reach ``_apply_bootstrap`` whenever the
host-local layer is not fully initialized (``is_initialized() == False``).
That path unconditionally rewrote ``local.yaml`` with the bare stub,
destroying a hand-edited host-local config with no confirm and no backup
when the ``~/.local/share/setforge/host-local/`` dir was absent.

The fix snapshots a non-pristine-stub ``local.yaml`` to a timestamped
``.bak`` before the overwrite. These tests fail on the old (clobbering)
behavior and pass with the backup guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.binaries import _STUB_TEMPLATE
from setforge.cli import app
from setforge.cli._init_helpers import host_local_dir_path

# A customized local.yaml: carries user content (a source: block + a custom
# top-level key) but NOT an active binaries: override — an override pointing at
# a non-existent path would abort probe_environment() before init's logic runs,
# which is orthogonal to the clobber behavior under test.
_CUSTOM_LOCAL_YAML = (
    "# setforge host-local config\n"
    "source:\n"
    "  kind: path\n"
    '  path: "/some/hand/edited/config-repo"\n'
    "custom: marker\n"
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-point ``$HOME`` and every module-bound LOCAL_CONFIG_PATH at tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    local_yaml = tmp_path / ".config" / "setforge" / "local.yaml"
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local_yaml)
    monkeypatch.setattr("setforge.cli._init_helpers.LOCAL_CONFIG_PATH", local_yaml)
    monkeypatch.setattr("setforge.cli.init.LOCAL_CONFIG_PATH", local_yaml)
    monkeypatch.setattr("setforge.cli._config_repo.LOCAL_CONFIG_PATH", local_yaml)
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local_yaml)
    return tmp_path


def _write_custom_local_yaml(home: Path) -> Path:
    """Write a customized local.yaml WITHOUT creating the host-local dir.

    This is the dangerous combination: ``is_initialized()`` is False (no
    host-local dir) but the file carries user content the overwrite path
    must not discard.
    """
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    local_yaml = cfg / "local.yaml"
    local_yaml.write_text(_CUSTOM_LOCAL_YAML, encoding="utf-8")
    assert not host_local_dir_path().exists()  # guard: not-initialized state
    return local_yaml


def _backup_text(local_yaml: Path) -> str | None:
    """Return the content of a ``local.yaml.bak.*`` sibling, if any."""
    backups = list(local_yaml.parent.glob("local.yaml.bak.*"))
    if not backups:
        return None
    assert len(backups) == 1, backups
    return backups[0].read_text(encoding="utf-8")


def _assert_marker_survived(local_yaml: Path) -> None:
    """Assert ``custom: marker`` survives in the live file or a .bak snapshot."""
    live = local_yaml.read_text(encoding="utf-8")
    backup = _backup_text(local_yaml)
    survived = "custom: marker" in live or (
        backup is not None and "custom: marker" in backup
    )
    assert survived, f"custom content lost — live={live!r} backup={backup!r}"


def test_config_repo_does_not_clobber_custom_local_yaml_when_host_local_missing(
    home: Path,
) -> None:
    """init --config-repo must preserve (or back up) a custom local.yaml.

    With a customized local.yaml present but the host-local dir absent,
    the old behavior overwrote the file with the bare stub. The fix backs
    the custom content up to a ``.bak`` sibling before rewriting.
    """
    local_yaml = _write_custom_local_yaml(home)
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", "--config-repo", "--no-prompt"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    _assert_marker_survived(local_yaml)


def test_bare_init_does_not_clobber_custom_local_yaml_when_host_local_missing(
    home: Path,
) -> None:
    """Bare ``init --no-prompt`` must not discard a custom local.yaml unbacked."""
    local_yaml = _write_custom_local_yaml(home)
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--no-prompt"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    _assert_marker_survived(local_yaml)


def test_bare_init_backs_up_then_writes_fresh_stub(home: Path) -> None:
    """The backup holds the OLD custom content; live holds the fresh stub."""
    local_yaml = _write_custom_local_yaml(home)
    runner = CliRunner()
    runner.invoke(app, ["init", "--no-prompt"], catch_exceptions=False)

    backup = _backup_text(local_yaml)
    assert backup is not None, "expected a local.yaml.bak.* snapshot"
    assert "custom: marker" in backup
    # Live file is now the freshly-written stub (host-local bootstrap ran).
    assert local_yaml.read_text(encoding="utf-8").startswith(_STUB_TEMPLATE)
    assert host_local_dir_path().exists()


def test_pristine_stub_is_not_backed_up(home: Path) -> None:
    """A pristine stub (root-callback default) must NOT spawn a .bak noise file."""
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    local_yaml = cfg / "local.yaml"
    local_yaml.write_text(_STUB_TEMPLATE, encoding="utf-8")
    assert not host_local_dir_path().exists()
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--no-prompt"], catch_exceptions=False)
    assert result.exit_code == 0, result.output

    assert _backup_text(local_yaml) is None  # no spurious backup
    assert local_yaml.read_text(encoding="utf-8").startswith(_STUB_TEMPLATE)
