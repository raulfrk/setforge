"""Tests for setforge/source.py — config-source discovery layer."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from setforge.errors import ConfigError, NoSourceConfigured, SourceNotCloned
from setforge.source import (
    CONFIG_FILENAME,
    DEFAULT_CLONE_ROOT,
    ENV_VAR,
    GitSource,
    PathSource,
    _load_local_source_config,
    resolve_source,
    resolve_source_dir,
    validate_source_dir,
)


def _write_local_yaml(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _write_source_dir(tmp_path: Path, name: str = "src") -> Path:
    """Create a directory with a stub ``my_setup.yaml`` inside."""
    src = tmp_path / name
    src.mkdir()
    (src / CONFIG_FILENAME).write_text("version: 1\n", encoding="utf-8")
    return src


# ---------------------------------------------------------------------------
# Schema: PathSource / GitSource discriminator + list rejection
# ---------------------------------------------------------------------------


class TestSchema:
    """Pydantic Source schema validates the kind-discriminator union."""

    def test_path_source_accepts_path_kind(self, tmp_path: Path) -> None:
        src = PathSource(kind="path", path=tmp_path)
        assert src.kind == "path"
        assert src.path == tmp_path

    def test_git_source_defaults_ref_to_main(self) -> None:
        src = GitSource(kind="git", url="git@github.com:r/x.git")
        assert src.ref == "main"

    def test_git_source_display_name_strips_dot_git(self) -> None:
        src = GitSource(kind="git", url="git@github.com:raulfrk/dotfiles.git")
        assert src.display_name == "dotfiles"

    def test_git_source_resolved_clone_dest_defaults_to_xdg(self) -> None:
        src = GitSource(kind="git", url="git@github.com:r/foo.git")
        assert src.resolved_clone_dest == DEFAULT_CLONE_ROOT / "foo"

    def test_git_source_resolved_clone_dest_honors_override(
        self, tmp_path: Path
    ) -> None:
        src = GitSource(
            kind="git",
            url="git@github.com:r/foo.git",
            clone_dest=tmp_path / "custom",
        )
        assert src.resolved_clone_dest == tmp_path / "custom"

    def test_local_source_config_rejects_list_shaped_source(
        self, tmp_path: Path
    ) -> None:
        cfg = _write_local_yaml(
            tmp_path / "local.yaml",
            "source:\n  - kind: path\n    path: /a\n  - kind: path\n    path: /b\n",
        )
        with pytest.raises(ValueError, match="must be a single mapping"):
            _load_local_source_config(cfg)

    def test_local_source_config_rejects_unknown_kind(self, tmp_path: Path) -> None:
        cfg = _write_local_yaml(
            tmp_path / "local.yaml",
            "source:\n  kind: ftp\n  path: /a\n",
        )
        with pytest.raises(ValidationError):
            _load_local_source_config(cfg)

    def test_local_source_config_rejects_extra_fields(self, tmp_path: Path) -> None:
        cfg = _write_local_yaml(
            tmp_path / "local.yaml",
            "source:\n  kind: path\n  path: /a\n  bogus: yes\n",
        )
        with pytest.raises(ValidationError, match="bogus"):
            _load_local_source_config(cfg)


# ---------------------------------------------------------------------------
# Loader: _load_local_source_config
# ---------------------------------------------------------------------------


class TestLoadLocalSourceConfig:
    """``_load_local_source_config`` parses the ``source:`` block."""

    def test_absent_file_returns_empty_config(self, tmp_path: Path) -> None:
        result = _load_local_source_config(tmp_path / "nope.yaml")
        assert result.source is None

    def test_empty_file_returns_empty_config(self, tmp_path: Path) -> None:
        cfg = _write_local_yaml(tmp_path / "local.yaml", "")
        result = _load_local_source_config(cfg)
        assert result.source is None

    def test_missing_source_key_returns_empty_config(self, tmp_path: Path) -> None:
        cfg = _write_local_yaml(
            tmp_path / "local.yaml",
            "binaries:\n  code: /bin/code\n",
        )
        result = _load_local_source_config(cfg)
        assert result.source is None

    def test_path_source_loads(self, tmp_path: Path) -> None:
        cfg = _write_local_yaml(
            tmp_path / "local.yaml",
            "source:\n  kind: path\n  path: /tmp/x\n",
        )
        result = _load_local_source_config(cfg)
        assert isinstance(result.source, PathSource)
        assert result.source.path == Path("/tmp/x")

    def test_git_source_loads(self, tmp_path: Path) -> None:
        cfg = _write_local_yaml(
            tmp_path / "local.yaml",
            "source:\n  kind: git\n  url: git@github.com:r/x.git\n  ref: dev\n",
        )
        result = _load_local_source_config(cfg)
        assert isinstance(result.source, GitSource)
        assert result.source.url == "git@github.com:r/x.git"
        assert result.source.ref == "dev"

    def test_malformed_yaml_raises_config_error(self, tmp_path: Path) -> None:
        cfg = _write_local_yaml(tmp_path / "local.yaml", "source:\n  - [bad\n")
        with pytest.raises(ConfigError, match="malformed YAML"):
            _load_local_source_config(cfg)

    def test_non_mapping_top_level_raises_config_error(self, tmp_path: Path) -> None:
        cfg = _write_local_yaml(tmp_path / "local.yaml", "- just-a-list\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            _load_local_source_config(cfg)


# ---------------------------------------------------------------------------
# 4-layer precedence: resolve_source
# ---------------------------------------------------------------------------


class TestResolveSourcePrecedence:
    """``resolve_source`` walks 4 layers, first non-empty wins entirely."""

    def test_cli_flag_wins_over_env(self, tmp_path: Path) -> None:
        cli = tmp_path / "cli_src"
        env_path = tmp_path / "env_src"
        result = resolve_source(
            cli_path=cli,
            env={ENV_VAR: str(env_path)},
            local_config_path=tmp_path / "nope.yaml",
            cwd=tmp_path,
        )
        assert isinstance(result, PathSource)
        assert result.path == cli

    def test_cli_flag_wins_over_local_yaml(self, tmp_path: Path) -> None:
        cli = tmp_path / "cli_src"
        cfg = _write_local_yaml(
            tmp_path / "local.yaml",
            "source:\n  kind: path\n  path: /tmp/yaml_src\n",
        )
        result = resolve_source(
            cli_path=cli,
            env={},
            local_config_path=cfg,
            cwd=tmp_path,
        )
        assert isinstance(result, PathSource)
        assert result.path == cli

    def test_env_wins_over_local_yaml(self, tmp_path: Path) -> None:
        env_path = tmp_path / "env_src"
        cfg = _write_local_yaml(
            tmp_path / "local.yaml",
            "source:\n  kind: path\n  path: /tmp/yaml_src\n",
        )
        result = resolve_source(
            cli_path=None,
            env={ENV_VAR: str(env_path)},
            local_config_path=cfg,
            cwd=tmp_path,
        )
        assert isinstance(result, PathSource)
        assert result.path == env_path

    def test_env_empty_string_treated_as_unset(self, tmp_path: Path) -> None:
        """``SETFORGE_SOURCE=`` (empty) falls through to next layer."""
        cwd_src = _write_source_dir(tmp_path)
        result = resolve_source(
            cli_path=None,
            env={ENV_VAR: ""},
            local_config_path=tmp_path / "nope.yaml",
            cwd=cwd_src,
        )
        assert isinstance(result, PathSource)
        assert result.path == cwd_src

    def test_local_yaml_wins_over_cwd_fallback(self, tmp_path: Path) -> None:
        cwd_src = _write_source_dir(tmp_path, "cwd_src")
        cfg = _write_local_yaml(
            tmp_path / "local.yaml",
            "source:\n  kind: path\n  path: /tmp/yaml_src\n",
        )
        result = resolve_source(
            cli_path=None,
            env={},
            local_config_path=cfg,
            cwd=cwd_src,
        )
        assert isinstance(result, PathSource)
        assert result.path == Path("/tmp/yaml_src")

    def test_cwd_fallback_when_my_setup_yaml_present(self, tmp_path: Path) -> None:
        cwd_src = _write_source_dir(tmp_path)
        result = resolve_source(
            cli_path=None,
            env={},
            local_config_path=tmp_path / "nope.yaml",
            cwd=cwd_src,
        )
        assert isinstance(result, PathSource)
        assert result.path == cwd_src

    def test_no_layer_produces_source_raises(self, tmp_path: Path) -> None:
        # cwd has no my_setup.yaml; no other layers populated.
        empty_cwd = tmp_path / "empty_cwd"
        empty_cwd.mkdir()
        with pytest.raises(NoSourceConfigured) as excinfo:
            resolve_source(
                cli_path=None,
                env={},
                local_config_path=tmp_path / "nope.yaml",
                cwd=empty_cwd,
            )
        msg = str(excinfo.value)
        assert "1. CLI flag --source" in msg
        assert "2. env SETFORGE_SOURCE" in msg
        assert "3." in msg
        assert "source:" in msg
        assert "4. CWD fallback" in msg


# ---------------------------------------------------------------------------
# resolve_source_dir + validate_source_dir
# ---------------------------------------------------------------------------


class TestResolveSourceDir:
    """``resolve_source_dir`` returns the on-disk directory."""

    def test_path_source_returns_path(self, tmp_path: Path) -> None:
        src = PathSource(kind="path", path=tmp_path)
        assert resolve_source_dir(src) == tmp_path

    def test_path_source_expands_tilde(self) -> None:
        src = PathSource(kind="path", path=Path("~/dotfiles"))
        assert resolve_source_dir(src) == Path.home() / "dotfiles"

    def test_git_source_returns_resolved_clone_dest_when_exists(
        self, tmp_path: Path
    ) -> None:
        clone = tmp_path / "clone"
        clone.mkdir()
        src = GitSource(kind="git", url="git@github.com:r/x.git", clone_dest=clone)
        assert resolve_source_dir(src) == clone

    def test_git_source_raises_when_clone_missing(self, tmp_path: Path) -> None:
        clone = tmp_path / "absent"
        src = GitSource(kind="git", url="git@github.com:r/x.git", clone_dest=clone)
        with pytest.raises(SourceNotCloned, match="not cloned"):
            resolve_source_dir(src)


class TestValidateSourceDir:
    """``validate_source_dir`` checks for ``my_setup.yaml`` in the source."""

    def test_returns_my_setup_yaml_path(self, tmp_path: Path) -> None:
        src_dir = _write_source_dir(tmp_path)
        src = PathSource(kind="path", path=src_dir)
        result = validate_source_dir(src)
        assert result == src_dir / CONFIG_FILENAME

    def test_raises_config_error_when_my_setup_yaml_missing(
        self, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        src = PathSource(kind="path", path=empty_dir)
        with pytest.raises(ConfigError, match=r"does not contain my_setup\.yaml"):
            validate_source_dir(src)

    def test_propagates_source_not_cloned_for_git_source(self, tmp_path: Path) -> None:
        src = GitSource(
            kind="git",
            url="git@github.com:r/x.git",
            clone_dest=tmp_path / "missing",
        )
        with pytest.raises(SourceNotCloned):
            validate_source_dir(src)


# ---------------------------------------------------------------------------
# resolve_source with env mapping (live env not used — tests inject)
# ---------------------------------------------------------------------------


class TestResolveSourceWithEnv:
    """env is passed in as a Mapping so tests don't touch os.environ."""

    def test_env_arg_accepts_dict(self, tmp_path: Path) -> None:
        env: Mapping[str, str] = {ENV_VAR: str(tmp_path)}
        result = resolve_source(
            cli_path=None,
            env=env,
            local_config_path=tmp_path / "nope.yaml",
            cwd=tmp_path,
        )
        assert isinstance(result, PathSource)
        assert result.path == tmp_path
