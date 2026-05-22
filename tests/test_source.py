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
    SourceKind,
    _load_local_source_config,
    _LocalTrackedFileOverlay,
    resolve_source,
    resolve_source_dir,
    validate_source_dir,
)


def _write_local_yaml(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _write_source_dir(tmp_path: Path, name: str = "src") -> Path:
    """Create a directory with a stub ``setforge.yaml`` inside."""
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
        src = PathSource(kind=SourceKind.PATH, path=tmp_path)
        assert src.kind == "path"
        assert src.path == tmp_path

    def test_git_source_defaults_ref_to_main(self) -> None:
        src = GitSource(kind=SourceKind.GIT, url="git@github.com:r/x.git")
        assert src.ref == "main"

    def test_git_source_display_name_strips_dot_git(self) -> None:
        src = GitSource(kind=SourceKind.GIT, url="git@github.com:raulfrk/dotfiles.git")
        assert src.display_name == "dotfiles"

    def test_git_source_resolved_clone_dest_defaults_to_xdg(self) -> None:
        src = GitSource(kind=SourceKind.GIT, url="git@github.com:r/foo.git")
        assert src.resolved_clone_dest == DEFAULT_CLONE_ROOT / "foo"

    def test_git_source_resolved_clone_dest_honors_override(
        self, tmp_path: Path
    ) -> None:
        src = GitSource(
            kind=SourceKind.GIT,
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

    def test_cwd_fallback_when_setforge_yaml_present(self, tmp_path: Path) -> None:
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
        # cwd has no setforge.yaml; no other layers populated.
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
        src = PathSource(kind=SourceKind.PATH, path=tmp_path)
        assert resolve_source_dir(src) == tmp_path

    def test_path_source_expands_tilde(self) -> None:
        src = PathSource(kind=SourceKind.PATH, path=Path("~/dotfiles"))
        assert resolve_source_dir(src) == Path.home() / "dotfiles"

    def test_git_source_returns_resolved_clone_dest_when_exists(
        self, tmp_path: Path
    ) -> None:
        clone = tmp_path / "clone"
        clone.mkdir()
        src = GitSource(
            kind=SourceKind.GIT, url="git@github.com:r/x.git", clone_dest=clone
        )
        assert resolve_source_dir(src) == clone

    def test_git_source_raises_when_clone_missing(self, tmp_path: Path) -> None:
        clone = tmp_path / "absent"
        src = GitSource(
            kind=SourceKind.GIT, url="git@github.com:r/x.git", clone_dest=clone
        )
        with pytest.raises(SourceNotCloned, match="not cloned"):
            resolve_source_dir(src)


class TestValidateSourceDir:
    """``validate_source_dir`` checks for ``setforge.yaml`` in the source."""

    def test_returns_setforge_yaml_path(self, tmp_path: Path) -> None:
        src_dir = _write_source_dir(tmp_path)
        src = PathSource(kind=SourceKind.PATH, path=src_dir)
        result = validate_source_dir(src)
        assert result == src_dir / CONFIG_FILENAME

    def test_raises_config_error_when_setforge_yaml_missing(
        self, tmp_path: Path
    ) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        src = PathSource(kind=SourceKind.PATH, path=empty_dir)
        with pytest.raises(ConfigError, match=r"does not contain setforge\.yaml"):
            validate_source_dir(src)

    def test_propagates_source_not_cloned_for_git_source(self, tmp_path: Path) -> None:
        src = GitSource(
            kind=SourceKind.GIT,
            url="git@github.com:r/x.git",
            clone_dest=tmp_path / "missing",
        )
        with pytest.raises(SourceNotCloned):
            validate_source_dir(src)

    def test_raises_migration_error_when_only_legacy_my_setup_yaml_present(
        self, tmp_path: Path
    ) -> None:
        """Legacy ``my_setup.yaml`` triggers a ``git mv`` migration hint.

        Mirrors the legacy-namespace detector pattern in
        :func:`setforge.sections.detect_legacy_namespace_markers`: when
        the parser would otherwise raise a generic "not found" error,
        recognize the old name and emit an actionable migration recipe
        instead.
        """
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "my_setup.yaml").write_text("profiles: {}\n")
        src = PathSource(kind=SourceKind.PATH, path=src_dir)
        with pytest.raises(
            ConfigError,
            match=(
                r"contains a legacy 'my_setup\.yaml'.*"
                r"git mv my_setup\.yaml setforge\.yaml"
            ),
        ):
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


# ---------------------------------------------------------------------------
# _LocalTrackedFileOverlay: host-local mode / dst / symlink_target (setforge-m3qx)
# ---------------------------------------------------------------------------


class TestLocalTrackedFileOverlayHostLocalOverrides:
    """Validator surface for the three host-local override fields.

    Per SPEC 7 / setforge-m3qx: ``mode`` (chmod) + ``dst`` (retarget
    install path) + ``symlink_target`` (install as symlink) extend
    :class:`_LocalTrackedFileOverlay`. ``mode`` and ``symlink_target``
    are mutually exclusive (chmod-on-symlink follows the link). ``mode``
    is bounded ``0 <= mode <= 0o7777`` at the parse layer; the setuid
    (0o4000) and setgid (0o2000) bits are refused for security so the
    effective upper bound is ``0o1777`` (sticky bit still permitted)
    — mirrors :func:`TrackedFile._validate_mode`. ``dst`` forbids
    ``$VAR`` env-var references — expansion happens at deploy time
    via ``Path.expanduser`` only.
    """

    def test_rejects_mode_with_symlink_target(self) -> None:
        """mode + symlink_target are mutually exclusive (footgun semantics)."""
        with pytest.raises(ValueError, match=r"mutually exclusive"):
            _LocalTrackedFileOverlay(mode=0o755, symlink_target=Path("/tmp/x"))

    def test_rejects_mode_out_of_range(self) -> None:
        """mode must be in 0..0o7777 (4095 decimal)."""
        with pytest.raises(ValueError, match=r"must be in 0\.\.0o7777"):
            _LocalTrackedFileOverlay(mode=0o10000)

    def test_rejects_typo_field_via_extra_forbid(self) -> None:
        """_STRICT extra='forbid' catches typo'd field names (e.g. modee)."""
        with pytest.raises(ValidationError):
            _LocalTrackedFileOverlay(modee=0o755)  # type: ignore[call-arg]

    def test_accepts_octal_mode(self) -> None:
        """mode: 0o755 (493 decimal) is in-range; no exception."""
        ovl = _LocalTrackedFileOverlay(mode=0o755)
        assert ovl.mode == 0o755

    def test_accepts_each_field_independently(self) -> None:
        """mode alone, dst alone, symlink_target alone — each accepted."""
        assert _LocalTrackedFileOverlay(mode=0o644).mode == 0o644
        assert _LocalTrackedFileOverlay(dst=Path("~/foo")).dst == Path("~/foo")
        sym_ovl = _LocalTrackedFileOverlay(symlink_target=Path("/usr/local/foo"))
        assert sym_ovl.symlink_target == Path("/usr/local/foo")

    def test_old_shape_still_parses(self) -> None:
        """Overlay with no new fields parses (backward compat for hosts
        that haven't adopted setforge-m3qx overrides)."""
        ovl = _LocalTrackedFileOverlay()
        assert ovl.mode is None
        assert ovl.dst is None
        assert ovl.symlink_target is None

    def test_rejects_env_var_in_dst(self) -> None:
        """dst must not reference $VAR-style env vars (out of contract)."""
        with pytest.raises(ValueError, match=r"\$"):
            _LocalTrackedFileOverlay(dst=Path("$HOME/foo"))

    def test_accepts_mode_zero(self) -> None:
        """Boundary: mode == 0 is in-range (degenerate but valid POSIX bits)."""
        ovl = _LocalTrackedFileOverlay(mode=0)
        assert ovl.mode == 0

    def test_accepts_mode_max(self) -> None:
        """Boundary: mode == 0o1777 (the highest accepted value).

        The parse-layer cap is 0o7777 (12-bit POSIX surface) but the
        setuid (0o4000) + setgid (0o2000) bits are refused for
        security (mirrors :func:`TrackedFile._validate_mode`). The
        sticky bit (0o1000) is still permitted, so 0o1777 is the
        maximum accepted value.
        """
        ovl = _LocalTrackedFileOverlay(mode=0o1777)
        assert ovl.mode == 0o1777

    def test_rejects_setuid_bit(self) -> None:
        """mode with setuid (0o4000) is refused at the overlay layer
        with a clear message — mirrors :func:`TrackedFile._validate_mode`
        so the merged-revalidate path in
        :func:`apply_host_local_tracked_file_overrides` cannot surface
        a less-clear ValidationError for the same input."""
        with pytest.raises(ValueError, match=r"setuid/setgid bits"):
            _LocalTrackedFileOverlay(mode=0o4755)

    def test_rejects_setgid_bit(self) -> None:
        """mode with setgid (0o2000) is refused with the same message
        as setuid — both are filtered by the ``mode & 0o6000`` check."""
        with pytest.raises(ValueError, match=r"setuid/setgid bits"):
            _LocalTrackedFileOverlay(mode=0o2755)

    def test_rejects_negative_mode(self) -> None:
        """Boundary: mode < 0 is out-of-range."""
        with pytest.raises(ValueError, match=r"must be in 0\.\.0o7777"):
            _LocalTrackedFileOverlay(mode=-1)

    def test_rejects_scalarint_yaml11_octal_shape(self) -> None:
        """ScalarInt (non-OctalInt) is rejected with the canonical
        "use 0o755" hint.

        Defense-in-depth at the direct-construction layer:
        :class:`_LocalTrackedFileOverlay`'s safe-yaml loader strips
        the OctalInt/ScalarInt distinction (both ``0755`` and ``755``
        parse to plain ``int(755)``); but a Python caller constructing
        the overlay with a ``ScalarInt`` argument still gets the strict
        rejection — mirrors :func:`TrackedFile._validate_mode`.
        """
        from ruamel.yaml.scalarint import ScalarInt

        # ScalarInt(755) is the shape ruamel.yaml's round-trip loader
        # emits for the YAML-1.1 ``mode: 0755`` form.
        with pytest.raises(ValueError, match=r"YAML-1\.1-style"):
            _LocalTrackedFileOverlay(mode=ScalarInt(755))

    def test_rejects_bool_mode(self) -> None:
        """Bool is rejected; isinstance(True, int) is True so the
        bool gate must run before the int gate."""
        with pytest.raises(ValueError, match=r"not bool"):
            _LocalTrackedFileOverlay(mode=True)  # type: ignore[arg-type]
