"""Regression tests: a stub + hand-appended source: block is NOT pristine.

Audit finding ``init_stub_appended``: ``_local_yaml_is_pristine_stub`` used a
bare ``text.startswith(_STUB_TEMPLATE)`` check. Because the stub template is a
PREFIX of any file that has had a ``source:``/``plugins:``/``extensions:``
block appended after it — exactly what the stub's own instructions tell users
to do — such a file was misclassified as pristine. In the not-initialized
window (``is_initialized() == False``), ``_apply_bootstrap(force=False)`` then
overwrote it with a fresh stub and made NO ``.bak``, silently discarding the
user's appended source/overlay config.

The fix requires the suffix after the stub to be empty or an init-generated,
marker-tagged source block. These tests fail on the old (startswith)
behavior and pass with the suffix guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.binaries import _STUB_TEMPLATE
from setforge.cli import app
from setforge.cli import init as init_mod
from setforge.cli._init_helpers import host_local_dir_path

# A stub with a hand-written source: block appended at the end, following the
# stub's own instructions. This is a PREFIX-of-stub file: text.startswith(stub)
# is True, so the old check wrongly classified it as pristine.
_STUB_PLUS_HAND_SOURCE = (
    _STUB_TEMPLATE + "\nsource:\n  kind: path\n  path: /some/hand/edited/repo\n"
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


def _write_stub_plus_hand_source(home: Path) -> Path:
    """Write a stub + hand-appended source: block, host-local dir absent."""
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    local_yaml = cfg / "local.yaml"
    local_yaml.write_text(_STUB_PLUS_HAND_SOURCE, encoding="utf-8")
    assert not host_local_dir_path().exists()  # guard: not-initialized state
    return local_yaml


def _backup_text(local_yaml: Path) -> str | None:
    """Return the content of a ``local.yaml.bak.*`` sibling, if any."""
    backups = list(local_yaml.parent.glob("local.yaml.bak.*"))
    if not backups:
        return None
    assert len(backups) == 1, backups
    return backups[0].read_text(encoding="utf-8")


def test_stub_plus_hand_source_predicate_non_pristine(home: Path) -> None:
    """Predicate: a stub with a hand-appended source: block is customized."""
    _write_stub_plus_hand_source(home)
    # The bug: startswith(_STUB_TEMPLATE) is True (prefix match) yet the file
    # carries user customization, so the predicate must classify it non-pristine.
    assert _STUB_PLUS_HAND_SOURCE.startswith(_STUB_TEMPLATE)  # the old trap
    assert init_mod._local_yaml_is_pristine_stub() is False


def test_bare_init_preserves_or_backs_up_hand_appended_source(home: Path) -> None:
    """Bare ``init --no-prompt`` must not discard a hand-appended source: block.

    With a stub + hand-written source: block present but the host-local dir
    absent, the old behavior overwrote with a bare stub and made no backup.
    The fix backs the customized content up before rewriting.
    """
    local_yaml = _write_stub_plus_hand_source(home)
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--no-prompt"], catch_exceptions=False)
    assert result.exit_code == 0, result.output

    live = local_yaml.read_text(encoding="utf-8")
    backup = _backup_text(local_yaml)
    survived = "/some/hand/edited/repo" in live or (
        backup is not None and "/some/hand/edited/repo" in backup
    )
    assert survived, f"hand-appended source lost — live={live!r} backup={backup!r}"


def test_bare_init_backup_holds_hand_appended_source(home: Path) -> None:
    """The .bak snapshot must hold the OLD hand-appended source: block."""
    local_yaml = _write_stub_plus_hand_source(home)
    runner = CliRunner()
    runner.invoke(app, ["init", "--no-prompt"], catch_exceptions=False)

    backup = _backup_text(local_yaml)
    assert backup is not None, "expected a local.yaml.bak.* snapshot"
    assert "/some/hand/edited/repo" in backup
    assert host_local_dir_path().exists()


def test_init_generated_source_block_stays_pristine(home: Path) -> None:
    """A stub + init-generated (marker-tagged) source block is still pristine.

    The marker comment ``# Pre-configured by `setforge init`` is what init
    itself writes; such a file carries no user customization and must NOT
    spawn a spurious .bak.
    """
    from setforge.cli.init import SourceChoice, SourceSpec, _build_source_block

    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    local_yaml = cfg / "local.yaml"
    generated = _build_source_block(
        SourceSpec(choice=SourceChoice.PATH, path=Path("/init/wrote/this"))
    )
    local_yaml.write_text(_STUB_TEMPLATE + generated, encoding="utf-8")
    assert not host_local_dir_path().exists()

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--no-prompt"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert _backup_text(local_yaml) is None  # no spurious backup
