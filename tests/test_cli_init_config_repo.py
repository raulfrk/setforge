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

from setforge.cli import app
from setforge.cli._config_repo import (
    ConfigRepoScaffoldError,
    default_config_repo_dir,
    local_yaml_has_source,
    scaffold_config_repo,
    write_starter_setforge_yaml,
)
from setforge.cli._init_helpers import host_local_dir_path
from setforge.migrations import current_expected_schema_version

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-point ``$HOME`` and every module-bound LOCAL_CONFIG_PATH at tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    local_yaml = tmp_path / ".config" / "setforge" / "local.yaml"
    monkeypatch.setattr("setforge.binaries.LOCAL_CONFIG_PATH", local_yaml)
    monkeypatch.setattr("setforge.cli._init_helpers.LOCAL_CONFIG_PATH", local_yaml)
    monkeypatch.setattr("setforge.cli.init.LOCAL_CONFIG_PATH", local_yaml)
    monkeypatch.setattr("setforge.cli._config_repo.LOCAL_CONFIG_PATH", local_yaml)
    # `compare` / `validate` resolve the source via the source module's
    # own LOCAL_CONFIG_PATH global; patch it so the wired source: block is
    # read from the test's local.yaml rather than the real $HOME one.
    monkeypatch.setattr("setforge.source.LOCAL_CONFIG_PATH", local_yaml)
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
    assert local_yaml.read_text(encoding="utf-8").count("source:") == 1  # no dup
    assert (repo / "setforge.yaml").read_bytes() == cfg_before  # not clobbered
    assert (repo / ".git" / "HEAD").read_bytes() == head_before  # no re-init


def test_init_config_repo_reuses_existing_local_yaml(home: Path) -> None:
    """When local.yaml exists, the host-local bootstrap is reused, not redone."""
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
