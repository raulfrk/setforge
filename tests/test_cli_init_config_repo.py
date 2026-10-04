"""Tests for ``setforge init --config-repo`` — config-repo scaffolding.

Covers the pure-logic helpers in :mod:`setforge.cli._config_repo` and the
``init --config-repo`` CLI flow: starter ``setforge.yaml`` that passes
``validate --all``, empty ``tracked/`` tree, ``.git`` init, ``source:``
wiring resolvable by ``compare``, idempotent second run (byte-identical
``local.yaml``, no duplicate source, no clobber, no git re-init), and the
git-absent clean error.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.binaries import _STUB_TEMPLATE
from setforge.cli import app
from setforge.cli import init as init_mod
from setforge.cli._config_repo import (
    ConfigRepoScaffoldError,
    default_config_repo_dir,
    local_yaml_has_source,
    scaffold_config_repo,
    write_starter_setforge_yaml,
)
from setforge.cli._init_helpers import host_local_dir_path
from setforge.migrations import current_expected_schema_version
from tests.conftest import redirect_local_config_path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-point ``$HOME`` and ``local.yaml`` at tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    local_yaml = tmp_path / ".config" / "setforge" / "local.yaml"
    redirect_local_config_path(monkeypatch, local_yaml)
    return tmp_path


# ---------------------------------------------------------------------------
# Pure-logic helpers
# ---------------------------------------------------------------------------


def test_default_config_repo_dir_under_projects(tmp_path: Path) -> None:
    target = default_config_repo_dir(home=tmp_path)
    assert target.parent == tmp_path / "projects"
    assert target.name.endswith("-config")


def test_write_starter_yaml_uses_current_schema_version(tmp_path: Path) -> None:
    written = write_starter_setforge_yaml(tmp_path)
    assert written is True
    text = (tmp_path / "setforge.yaml").read_text(encoding="utf-8")
    assert f'schema_version: "{current_expected_schema_version}"' in text
    assert "tracked_files: {}" in text


def test_write_starter_yaml_does_not_clobber_existing(tmp_path: Path) -> None:
    (tmp_path / "setforge.yaml").write_text("custom: marker\n", encoding="utf-8")
    written = write_starter_setforge_yaml(tmp_path)
    assert written is False
    assert "custom: marker" in (tmp_path / "setforge.yaml").read_text(encoding="utf-8")


def test_scaffold_creates_repo_tracked_and_yaml(tmp_path: Path) -> None:
    target = tmp_path / "cfg"
    result = scaffold_config_repo(target)
    assert result == target
    assert (target / ".git").is_dir()
    assert (target / "tracked").is_dir()
    assert not any((target / "tracked").iterdir())  # empty tree
    assert (target / "setforge.yaml").exists()


def test_scaffold_rejects_nonempty_non_repo_dir(tmp_path: Path) -> None:
    target = tmp_path / "cfg"
    target.mkdir()
    (target / "junk.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigRepoScaffoldError, match="non-empty"):
        scaffold_config_repo(target)


def test_scaffold_rejects_regular_file_target(tmp_path: Path) -> None:
    target = tmp_path / "cfg"
    target.write_text("occupied\n", encoding="utf-8")

    with pytest.raises(ConfigRepoScaffoldError, match="not a directory"):
        scaffold_config_repo(target)


def test_scaffold_rejects_missing_parent(tmp_path: Path) -> None:
    target = tmp_path / "no" / "such" / "parent" / "cfg"
    with pytest.raises(ConfigRepoScaffoldError, match="parent directory"):
        scaffold_config_repo(target)


def test_scaffold_idempotent_no_reinit(tmp_path: Path) -> None:
    target = tmp_path / "cfg"
    scaffold_config_repo(target)
    # Drop a marker into the .git dir; a re-init would not preserve a custom
    # HEAD, but our skip-if-repo guard leaves the existing repo untouched.
    head_before = (target / ".git" / "HEAD").read_bytes()
    (target / "tracked" / "keep.md").write_text("hi", encoding="utf-8")
    scaffold_config_repo(target)  # second run
    assert (target / ".git" / "HEAD").read_bytes() == head_before
    assert (target / "tracked" / "keep.md").exists()  # not wiped


def test_git_init_absent_raises_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("setforge.cli._config_repo.shutil.which", lambda _name: None)
    with pytest.raises(ConfigRepoScaffoldError, match="git not found"):
        scaffold_config_repo(tmp_path / "cfg")


def test_local_yaml_has_source_detects_block(tmp_path: Path) -> None:
    yaml = tmp_path / "local.yaml"
    yaml.write_text("source:\n  kind: path\n  path: /x\n", encoding="utf-8")
    assert local_yaml_has_source(yaml) is True


def test_local_yaml_has_source_false_when_absent(tmp_path: Path) -> None:
    assert local_yaml_has_source(tmp_path / "nope.yaml") is False


def test_local_yaml_has_source_false_when_no_key(tmp_path: Path) -> None:
    yaml = tmp_path / "local.yaml"
    yaml.write_text("# just a comment\nbinaries: {}\n", encoding="utf-8")
    assert local_yaml_has_source(yaml) is False


def test_local_yaml_has_source_wraps_malformed_yaml(tmp_path: Path) -> None:
    """A hand-corrupted local.yaml surfaces a clean error, not a parser traceback."""
    yaml = tmp_path / "local.yaml"
    yaml.write_text("source: [unterminated\n", encoding="utf-8")
    with pytest.raises(ConfigRepoScaffoldError, match="not valid YAML"):
        local_yaml_has_source(yaml)


# ---------------------------------------------------------------------------
# CLI flow — init --config-repo
# ---------------------------------------------------------------------------


def test_init_config_repo_scaffolds_and_validates(home: Path) -> None:
    """init --config-repo produces a repo whose setforge.yaml passes validate --all."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", "--config-repo", "--no-prompt"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output

    # Default target name derives from the host; assert the structure under
    # ~/projects exists rather than the exact name.
    repos = list((home / "projects").iterdir())
    assert len(repos) == 1, repos
    repo = repos[0]
    assert (repo / ".git").is_dir()
    assert (repo / "tracked").is_dir()
    assert not any((repo / "tracked").iterdir())

    cfg_yaml = repo / "setforge.yaml"
    text = cfg_yaml.read_text(encoding="utf-8")
    assert f'schema_version: "{current_expected_schema_version}"' in text

    # validate --all against the scaffolded config must pass.
    vresult = runner.invoke(
        app, ["validate", "--all", "--config", str(cfg_yaml)], catch_exceptions=False
    )
    assert vresult.exit_code == 0, vresult.output
    assert "ok" in vresult.output


def test_init_config_repo_wires_source_resolvable_by_compare(home: Path) -> None:
    """After init --config-repo, compare --profile=default resolves the source."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", "--config-repo", "--no-prompt"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output

    local_yaml = home / ".config" / "setforge" / "local.yaml"
    assert local_yaml_has_source(local_yaml) is True

    cresult = runner.invoke(app, ["compare", "--profile=default"])
    # compare resolves the source (no NoSourceConfigured / shape error) and
    # exits cleanly on an empty (drift-free) config.
    assert "NoSourceConfigured" not in cresult.output
    assert cresult.exit_code == 0, cresult.output


def test_init_config_repo_idempotent_second_run(home: Path) -> None:
    """A second run leaves local.yaml byte-identical and does not clobber yaml/git."""
    runner = CliRunner()
    runner.invoke(app, ["init", "--config-repo", "--no-prompt"], catch_exceptions=False)
    local_yaml = home / ".config" / "setforge" / "local.yaml"
    first_bytes = local_yaml.read_bytes()

    repo = next((home / "projects").iterdir())
    cfg_before = (repo / "setforge.yaml").read_bytes()
    head_before = (repo / ".git" / "HEAD").read_bytes()

    second = runner.invoke(
        app, ["init", "--config-repo", "--no-prompt"], catch_exceptions=False
    )
    assert second.exit_code == 0, second.output

    assert local_yaml.read_bytes() == first_bytes  # byte-identical
    text = local_yaml.read_text(encoding="utf-8")
    assert text.count("\nsource:") == 1  # no dup
    assert (repo / "setforge.yaml").read_bytes() == cfg_before  # not clobbered
    assert (repo / ".git" / "HEAD").read_bytes() == head_before  # no re-init


def test_init_config_repo_reuses_existing_local_yaml(home: Path) -> None:
    """When the host-local layer is already initialized, bootstrap is reused."""
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    (cfg / "local.yaml").write_text(
        "# setforge host-local config\ncustom: marker\n", encoding="utf-8"
    )
    host_local_dir_path().mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", "--config-repo", "--no-prompt"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    text = (cfg / "local.yaml").read_text(encoding="utf-8")
    assert "custom: marker" in text  # preserved
    assert "source:" in text  # source wired onto the reused file


def test_init_config_repo_preserves_local_yaml_mode(home: Path) -> None:
    """Wiring the source: block must not widen local.yaml's permission bits."""
    cfg = home / ".config" / "setforge"
    cfg.mkdir(parents=True)
    local_yaml = cfg / "local.yaml"
    # Use the sentinel header so is_initialized() is True and the existing
    # (0600) file is reused + appended to — the path _wire_source_block's
    # mode-preservation guards.
    local_yaml.write_text("# setforge host-local config\n", encoding="utf-8")
    local_yaml.chmod(0o600)
    host_local_dir_path().mkdir(parents=True)
    runner = CliRunner()
    runner.invoke(app, ["init", "--config-repo", "--no-prompt"], catch_exceptions=False)
    assert (local_yaml.stat().st_mode & 0o777) == 0o600


def test_init_config_repo_git_absent_errors_cleanly(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git-absent surfaces a clean error + exit 1, no traceback."""
    real_which = shutil.which

    def _which(name: str) -> str | None:
        if name == "git":
            return None
        return real_which(name)

    monkeypatch.setattr("setforge.cli._config_repo.shutil.which", _which)
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--config-repo", "--no-prompt"])
    assert result.exit_code == 1
    assert "git not found" in result.output


# ---------------------------------------------------------------------------
# Bare init unchanged (regression guard)
# ---------------------------------------------------------------------------


def test_bare_init_does_not_scaffold_config_repo(home: Path) -> None:
    """Bare init (no --config-repo) creates no projects/ config repo."""
    cfg = home / ".config" / "setforge"
    if (cfg / "local.yaml").exists():
        (cfg / "local.yaml").unlink()
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--no-prompt"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert not (home / "projects").exists()
    # No source block wired by bare init.
    local_yaml = cfg / "local.yaml"
    assert local_yaml_has_source(local_yaml) is False


def test_git_init_available_on_host() -> None:
    """Sanity: these tests rely on a real git binary; skip cleanly if absent."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    # Smoke that git init works at all (guards against a broken git).
    assert (
        subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=10
        ).returncode
        == 0
    )


# Regression tests: a stub + hand-appended source: block is NOT pristine.
#
# Audit finding ``init_stub_appended``: ``_local_yaml_is_pristine_stub`` used a
# bare ``text.startswith(_STUB_TEMPLATE)`` check. Because the stub template is a
# PREFIX of any file that has had a ``source:``/``plugins:``/``extensions:``
# block appended after it — exactly what the stub's own instructions tell users
# to do — such a file was misclassified as pristine. In the not-initialized
# window (``is_initialized() == False``), ``_apply_bootstrap(force=False)`` then
# overwrote it with a fresh stub and made NO ``.bak``, silently discarding the
# user's appended source/overlay config.
#
# The fix requires the suffix after the stub to be empty or an init-generated,
# marker-tagged source block. These tests fail on the old (startswith)
# behavior and pass with the suffix guard.

_STUB_PLUS_HAND_SOURCE = (
    _STUB_TEMPLATE + "\nsource:\n  kind: path\n  path: /some/hand/edited/repo\n"
)


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


# Regression tests: init must not silently clobber a customized local.yaml.
#
# Audit finding ``init_clobber_localyaml``: ``setforge init --config-repo``
# (and bare ``init --no-prompt``) reach ``_apply_bootstrap`` whenever the
# host-local layer is not fully initialized (``is_initialized() == False``).
# That path unconditionally rewrote ``local.yaml`` with the bare stub,
# destroying a hand-edited host-local config with no confirm and no backup
# when the ``~/.local/share/setforge/host-local/`` dir was absent.
#
# The fix snapshots a non-pristine-stub ``local.yaml`` to a timestamped
# ``.bak`` before the overwrite. These tests fail on the old (clobbering)
# behavior and pass with the backup guard.

_CUSTOM_LOCAL_YAML = (
    "# setforge host-local config\n"
    "source:\n"
    "  kind: path\n"
    '  path: "/some/hand/edited/config-repo"\n'
    "custom: marker\n"
)


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
