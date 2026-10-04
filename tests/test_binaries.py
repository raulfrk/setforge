"""Tests for setforge.binaries — host-local binary override resolver."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from setforge import binaries, paths
from setforge.binaries import ClaudeLocalConfig, HostLocalConfig, load_host_local_config
from setforge.config import ClaudeInstallMode
from setforge.errors import BinaryOverrideInvalid, ConfigError
from tests.conftest import redirect_local_config_path


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Clear CLI/env state.

    The conftest fixtures give every test an isolated home and ``local.yaml``,
    so production state is never touched. Env vars for overrides are unset so
    a polluted shell can't leak into tests.
    """
    binaries._cli_overrides.clear()
    for name in binaries.SUPPORTED_BINARIES:
        monkeypatch.delenv(
            f"{binaries._ENV_VAR_PREFIX}{name.upper()}{binaries._ENV_VAR_SUFFIX}",
            raising=False,
        )


def test_load_local_config_missing_returns_empty() -> None:
    assert binaries._load_local_config() == {}


def test_load_local_config_empty_file_returns_empty() -> None:
    paths.local_config_path().write_text("")
    assert binaries._load_local_config() == {}


def test_load_local_config_no_binaries_key_returns_empty() -> None:
    paths.local_config_path().write_text("other: true\n")
    assert binaries._load_local_config() == {}


def test_load_local_config_returns_binaries_mapping() -> None:
    paths.local_config_path().write_text(
        "binaries:\n  code: /custom/code\n  patch: /custom/patch\n"
    )
    assert binaries._load_local_config() == {
        "code": "/custom/code",
        "patch": "/custom/patch",
    }


def test_load_local_config_malformed_yaml_raises() -> None:
    paths.local_config_path().write_text("binaries:\n  code: [unterminated\n")
    with pytest.raises(ConfigError, match="malformed YAML"):
        binaries._load_local_config()


def test_load_local_config_binaries_not_a_mapping_raises() -> None:
    paths.local_config_path().write_text("binaries: a-string\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        binaries._load_local_config()


def test_load_local_config_top_level_not_a_mapping_raises() -> None:
    paths.local_config_path().write_text("- list\n- only\n")
    with pytest.raises(ConfigError, match="top-level"):
        binaries._load_local_config()


def test_env_overrides_none_set_returns_empty() -> None:
    assert binaries._env_overrides() == {}


def test_env_overrides_one_set(monkeypatch) -> None:
    monkeypatch.setenv("SETFORGE_CODE_BIN", "/env/code")
    assert binaries._env_overrides() == {"code": "/env/code"}


def test_env_overrides_all_three_set(monkeypatch) -> None:
    monkeypatch.setenv("SETFORGE_CODE_BIN", "/env/code")
    monkeypatch.setenv("SETFORGE_CLAUDE_BIN", "/env/claude")
    monkeypatch.setenv("SETFORGE_GITLEAKS_BIN", "/env/gitleaks")
    assert binaries._env_overrides() == {
        "code": "/env/code",
        "claude": "/env/claude",
        "gitleaks": "/env/gitleaks",
    }


def test_retired_patch_overrides_load_and_are_ignored(monkeypatch) -> None:
    """A ``patch`` entry from releases that used GNU patch breaks nothing."""
    paths.local_config_path().write_text("binaries:\n  patch: /missing/patch\n")
    monkeypatch.setenv("SETFORGE_PATCH_BIN", "/missing/patch")

    assert binaries._env_overrides() == {}
    assert dict(load_host_local_config().binaries) == {"patch": "/missing/patch"}
    binaries.resolve_binary("code")


def test_env_overrides_empty_string_treated_as_unset(monkeypatch) -> None:
    monkeypatch.setenv("SETFORGE_CODE_BIN", "")
    assert binaries._env_overrides() == {}


def test_set_cli_overrides_stores_provided_values() -> None:
    binaries.set_cli_overrides(code="/cli/code", gitleaks="/cli/gitleaks")
    assert binaries._cli_overrides == {
        "code": "/cli/code",
        "gitleaks": "/cli/gitleaks",
    }


def test_set_cli_overrides_none_skipped() -> None:
    binaries.set_cli_overrides(code=None, claude=None, gitleaks=None)
    assert binaries._cli_overrides == {}


def test_set_cli_overrides_replaces_prior() -> None:
    binaries.set_cli_overrides(code="/first")
    binaries.set_cli_overrides(claude="/second")
    assert binaries._cli_overrides == {"claude": "/second"}


def _make_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_validate_missing_path_raises(tmp_path) -> None:
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries._validate("code", str(tmp_path / "nope"), layer="cli")
    assert excinfo.value.layer == "cli"
    assert excinfo.value.binary == "code"
    assert excinfo.value.reason == "not found"


def test_validate_non_executable_raises(tmp_path) -> None:
    bin_path = tmp_path / "code"
    bin_path.write_text("not executable")
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries._validate("code", str(bin_path), layer="env")
    assert excinfo.value.layer == "env"
    assert excinfo.value.reason == "not executable"


def test_validate_returns_path_for_valid_executable(tmp_path) -> None:
    bin_path = _make_executable(tmp_path / "code")
    result = binaries._validate("code", str(bin_path), layer="config")
    assert result == bin_path


@pytest.mark.parametrize("layer", ["cli", "env", "config", "path"])
def test_relative_binary_path_survives_child_cwd_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layer: str
) -> None:
    import subprocess

    executable = _make_executable(tmp_path / "code")
    monkeypatch.chdir(tmp_path)
    if layer == "cli":
        binaries.set_cli_overrides(code="./code")
    elif layer == "env":
        monkeypatch.setenv("SETFORGE_CODE_BIN", "./code")
    elif layer == "config":
        paths.local_config_path().write_text("binaries:\n  code: ./code\n")
    else:
        monkeypatch.setattr(binaries.shutil, "which", lambda _name: "./code")
    resolved = binaries.resolve_binary("code")
    assert resolved == executable
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [str(resolved)], cwd=elsewhere, check=True, capture_output=True
    )
    assert result.returncode == 0


def test_resolve_falls_back_to_which(monkeypatch, tmp_path) -> None:
    fake = _make_executable(tmp_path / "code")
    monkeypatch.setattr(
        binaries.shutil, "which", lambda n: str(fake) if n == "code" else None
    )
    assert binaries.resolve_binary("code") == Path(str(fake))


def test_resolve_returns_none_when_unresolved(monkeypatch) -> None:
    monkeypatch.setattr(binaries.shutil, "which", lambda _: None)
    assert binaries.resolve_binary("code") is None


def test_resolve_config_layer(tmp_path) -> None:
    bin_path = _make_executable(tmp_path / "code")
    paths.local_config_path().write_text(f"binaries:\n  code: {bin_path}\n")
    assert binaries.resolve_binary("code") == bin_path


def test_resolve_env_overrides_config(monkeypatch, tmp_path) -> None:
    cfg_bin = _make_executable(tmp_path / "cfg-code")
    env_bin = _make_executable(tmp_path / "env-code")
    paths.local_config_path().write_text(f"binaries:\n  code: {cfg_bin}\n")
    monkeypatch.setenv("SETFORGE_CODE_BIN", str(env_bin))
    assert binaries.resolve_binary("code") == env_bin


def test_resolve_cli_overrides_env_and_config(monkeypatch, tmp_path) -> None:
    cfg_bin = _make_executable(tmp_path / "cfg-code")
    env_bin = _make_executable(tmp_path / "env-code")
    cli_bin = _make_executable(tmp_path / "cli-code")
    paths.local_config_path().write_text(f"binaries:\n  code: {cfg_bin}\n")
    monkeypatch.setenv("SETFORGE_CODE_BIN", str(env_bin))
    binaries.set_cli_overrides(code=str(cli_bin))
    assert binaries.resolve_binary("code") == cli_bin


def test_resolve_invalid_cli_override_raises(tmp_path) -> None:
    binaries.set_cli_overrides(code=str(tmp_path / "nope"))
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries.resolve_binary("code")
    assert excinfo.value.layer == "cli"


def test_resolve_invalid_env_override_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SETFORGE_CODE_BIN", str(tmp_path / "nope"))
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries.resolve_binary("code")
    assert excinfo.value.layer == "env"


def test_resolve_invalid_config_override_raises(tmp_path) -> None:
    paths.local_config_path().write_text(f"binaries:\n  code: {tmp_path / 'nope'}\n")
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries.resolve_binary("code")
    assert excinfo.value.layer == "config"


def test_resolve_empty_config_override_raises(tmp_path) -> None:
    """An empty-string config override is a broken override, not absent.

    It must surface as ``BinaryOverrideInvalid`` (matching the CLI layer
    and the resolver's stated contract) rather than silently falling
    through to ``shutil.which``.
    """
    paths.local_config_path().write_text('binaries:\n  code: ""\n')
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries.resolve_binary("code")
    assert excinfo.value.layer == "config"
    assert excinfo.value.reason == "empty path"


def test_resolve_empty_cli_override_raises() -> None:
    """An empty-string CLI override raises rather than resolving to cwd.

    ``Path("")`` is ``Path(".")``, which exists and is executable, so an
    empty override must be rejected explicitly for every layer.
    """
    binaries.set_cli_overrides(code="")
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries.resolve_binary("code")
    assert excinfo.value.layer == "cli"
    assert excinfo.value.reason == "empty path"


def test_uv_in_supported_binaries() -> None:
    """uv is a supported binary so its env/config overrides are honored."""
    assert "uv" in binaries.SUPPORTED_BINARIES


def test_resolve_uv_env_override(monkeypatch, tmp_path) -> None:
    """SETFORGE_UV_BIN overrides uv resolution instead of being ignored."""
    env_bin = _make_executable(tmp_path / "env-uv")
    monkeypatch.setenv("SETFORGE_UV_BIN", str(env_bin))
    assert binaries.resolve_binary("uv") == env_bin


def test_resolve_uv_config_override(tmp_path) -> None:
    """local.yaml binaries.uv overrides uv resolution instead of being ignored."""
    cfg_bin = _make_executable(tmp_path / "cfg-uv")
    paths.local_config_path().write_text(f"binaries:\n  uv: {cfg_bin}\n")
    assert binaries.resolve_binary("uv") == cfg_bin


def test_ensure_stub_creates_file_when_absent() -> None:
    assert not paths.local_config_path().exists()
    binaries.ensure_local_config_stub()
    assert paths.local_config_path().exists()
    text = paths.local_config_path().read_text(encoding="utf-8")
    assert "binaries:" in text
    assert text.startswith("# setforge host-local config")


def test_ensure_stub_creates_parent_directories(monkeypatch, tmp_path) -> None:
    nested = tmp_path / "deep" / "nested" / "local.yaml"
    redirect_local_config_path(monkeypatch, nested)
    binaries.ensure_local_config_stub()
    assert nested.exists()


def test_ensure_stub_does_not_overwrite_existing() -> None:
    paths.local_config_path().write_text("user content\n")
    binaries.ensure_local_config_stub()
    assert paths.local_config_path().read_text() == "user content\n"


def test_ensure_stub_is_idempotent() -> None:
    binaries.ensure_local_config_stub()
    first_mtime = paths.local_config_path().stat().st_mtime_ns
    binaries.ensure_local_config_stub()
    assert paths.local_config_path().stat().st_mtime_ns == first_mtime


def test_stderr_of_returns_stripped_stderr_when_present() -> None:
    exc = subprocess.CalledProcessError(
        1, ["claude"], stderr="  installation failed  \n"
    )
    assert binaries.stderr_of(exc) == "installation failed"


def test_stderr_of_falls_back_to_str_when_stderr_attr_is_none() -> None:
    exc = subprocess.TimeoutExpired(["claude"], 30)
    assert binaries.stderr_of(exc) == str(exc)


def test_stderr_of_falls_back_to_str_when_stderr_is_whitespace_only() -> None:
    exc = subprocess.CalledProcessError(1, ["claude"], stderr="   \n  ")
    assert binaries.stderr_of(exc) == str(exc)


def test_stderr_of_falls_back_to_str_for_generic_exception() -> None:
    exc = ValueError("plain error")
    assert binaries.stderr_of(exc) == "plain error"


# ---------------------------------------------------------------------------
# HostLocalConfig + ClaudeLocalConfig (nen.15)
# ---------------------------------------------------------------------------


def test_host_local_config_missing_returns_defaults() -> None:
    """Missing local.yaml yields defaults — REGULAR install_mode, no binaries."""
    cfg = load_host_local_config()
    assert isinstance(cfg, HostLocalConfig)
    assert dict(cfg.binaries) == {}
    assert cfg.claude == ClaudeLocalConfig()
    assert cfg.claude.install_mode is ClaudeInstallMode.REGULAR


def test_host_local_config_empty_file_returns_defaults() -> None:
    """Empty YAML file is indistinguishable from a missing file."""
    paths.local_config_path().write_text("")
    cfg = load_host_local_config()
    assert dict(cfg.binaries) == {}
    assert cfg.claude.install_mode is ClaudeInstallMode.REGULAR


def test_host_local_config_binaries_only_keeps_claude_defaults() -> None:
    """Existing binaries-only files keep working — claude defaults fill in."""
    paths.local_config_path().write_text(
        "binaries:\n  code: /custom/code\n  patch: /custom/patch\n"
    )
    cfg = load_host_local_config()
    assert dict(cfg.binaries) == {"code": "/custom/code", "patch": "/custom/patch"}
    assert cfg.claude.install_mode is ClaudeInstallMode.REGULAR


def test_host_local_config_claude_block_only_keeps_binaries_default() -> None:
    """A claude-only block doesn't accidentally drop binaries' default."""
    paths.local_config_path().write_text("claude:\n  install_mode: local-clone\n")
    cfg = load_host_local_config()
    assert dict(cfg.binaries) == {}
    assert cfg.claude.install_mode is ClaudeInstallMode.LOCAL_CLONE


def test_host_local_config_claude_install_mode_regular_explicit() -> None:
    """Explicit ``install_mode: regular`` round-trips to the enum member."""
    paths.local_config_path().write_text("claude:\n  install_mode: regular\n")
    cfg = load_host_local_config()
    assert cfg.claude.install_mode is ClaudeInstallMode.REGULAR


def test_host_local_config_claude_block_with_no_install_mode_defaults() -> None:
    """A claude block without install_mode keeps the REGULAR default."""
    paths.local_config_path().write_text("claude: {}\n")
    cfg = load_host_local_config()
    assert cfg.claude.install_mode is ClaudeInstallMode.REGULAR


def test_host_local_config_claude_install_mode_bad_value_raises() -> None:
    """A garbage install_mode value names the file and the valid members."""
    paths.local_config_path().write_text("claude:\n  install_mode: garbage\n")
    with pytest.raises(ConfigError, match=r"claude\.install_mode"):
        load_host_local_config()


def test_host_local_config_claude_block_not_mapping_raises() -> None:
    """A scalar ``claude:`` block fails fast with a typed ConfigError."""
    paths.local_config_path().write_text("claude: not-a-mapping\n")
    with pytest.raises(ConfigError, match=r"'claude:'"):
        load_host_local_config()


def test_host_local_config_both_blocks_parsed_together() -> None:
    """Binaries + claude blocks coexist in one parse pass."""
    paths.local_config_path().write_text(
        "binaries:\n  code: /x/code\nclaude:\n  install_mode: local-clone\n"
    )
    cfg = load_host_local_config()
    assert dict(cfg.binaries) == {"code": "/x/code"}
    assert cfg.claude.install_mode is ClaudeInstallMode.LOCAL_CLONE


def test_host_local_config_malformed_yaml_raises() -> None:
    """YAML parse failure surfaces as ConfigError, not a raw YAMLError."""
    paths.local_config_path().write_text("claude:\n  install_mode: [unterminated\n")
    with pytest.raises(ConfigError, match="malformed YAML"):
        load_host_local_config()


def test_host_local_config_top_level_not_mapping_raises() -> None:
    """List-typed YAML root is rejected before block parsing runs."""
    paths.local_config_path().write_text("- a\n- b\n")
    with pytest.raises(ConfigError, match="top-level"):
        load_host_local_config()


def test_claude_local_config_default_install_mode_is_regular() -> None:
    """ClaudeLocalConfig() with no args defaults to REGULAR install_mode."""
    assert ClaudeLocalConfig().install_mode is ClaudeInstallMode.REGULAR


def test_host_local_config_frozen_dataclass_rejects_mutation() -> None:
    """HostLocalConfig is frozen — assigning to fields raises FrozenInstanceError."""
    cfg = HostLocalConfig()
    with pytest.raises(Exception, match=r"frozen|cannot assign"):
        # Intentional read-only-property assignment to assert frozen behaviour.
        cfg.claude = ClaudeLocalConfig(install_mode=ClaudeInstallMode.LOCAL_CLONE)  # type: ignore[misc]


def test_stub_template_documents_claude_install_mode() -> None:
    """Stub template surfaces the install_mode knob so users can discover it."""
    assert not paths.local_config_path().exists()
    binaries.ensure_local_config_stub()
    text = paths.local_config_path().read_text(encoding="utf-8")
    assert "claude:" in text
    assert "install_mode" in text
    assert "local-clone" in text


def test_stub_template_omits_unimplemented_knobs() -> None:
    """Stub must not advertise knobs no code reads (invites cargo-culting)."""
    binaries.ensure_local_config_stub()
    text = paths.local_config_path().read_text(encoding="utf-8")
    assert "claude_log_level" not in text
    assert "cache_max_age_days" not in text


def test_stub_template_still_valid_yaml() -> None:
    """Generated stub round-trips through the config loader after edits."""
    binaries.ensure_local_config_stub()
    cfg = load_host_local_config()
    assert isinstance(cfg, HostLocalConfig)
    assert cfg.claude.install_mode is ClaudeInstallMode.REGULAR


def test_host_local_config_both_blocks_malformed_binaries_error_wins() -> None:
    """When both blocks are malformed, the binaries error surfaces first.

    ``load_host_local_config`` builds ``HostLocalConfig(binaries=..., claude=...)``;
    Python evaluates the ``binaries`` argument before ``claude``, so the
    ``binaries:`` error short-circuits and the ``claude.install_mode`` error
    never fires. Swapping that call order would flip which message wins — this
    pins the current precedence so such a refactor fails loudly.
    """
    paths.local_config_path().write_text(
        "binaries: a-string\nclaude:\n  install_mode: garbage\n"
    )
    with pytest.raises(ConfigError, match=r"'binaries:'") as excinfo:
        load_host_local_config()
    assert "install_mode" not in str(excinfo.value)


def _stub_example_blocks() -> list[str]:
    blocks: list[list[str]] = []
    for line in binaries._STUB_TEMPLATE.splitlines():
        if line.startswith("# ") and line[2:3].isalpha() and line.endswith(":"):
            blocks.append([line[2:]])
        elif line.startswith("#  ") and blocks:
            blocks[-1].append(line[2:])
        elif not line.startswith("#  "):
            continue
    return ["\n".join(block) + "\n" for block in blocks if len(block) > 1]


def test_stub_has_examples_for_every_documented_block() -> None:
    keys = {block.split(":", 1)[0] for block in _stub_example_blocks()}
    assert keys >= {
        "binaries",
        "claude",
        "plugins",
        "extensions",
        "marketplaces",
        "tracked_files",
    }


@pytest.mark.parametrize("block", _stub_example_blocks())
def test_every_uncommented_stub_example_validates(block: str) -> None:
    from ruamel.yaml import YAML

    from setforge.cli.validate import _LocalConfig
    from setforge.source import _load_local_source_config

    path = paths.local_config_path()
    path.write_text(block, encoding="utf-8")

    _LocalConfig.model_validate(YAML(typ="safe").load(block))
    _load_local_source_config(path)
    load_host_local_config()


def test_tilde_in_binary_override_is_expanded(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    exe = _make_executable(home / "bin" / "code")
    monkeypatch.setenv("HOME", str(home))
    binaries.set_cli_overrides(code="~/bin/code")
    assert binaries.resolve_binary("code") == exe
    binaries._cli_overrides.clear()
    monkeypatch.setenv("SETFORGE_CODE_BIN", "~/bin/code")
    assert binaries.resolve_binary("code") == exe
    monkeypatch.delenv("SETFORGE_CODE_BIN")
    paths.local_config_path().write_text("binaries:\n  code: ~/bin/code\n")
    assert binaries.resolve_binary("code") == exe
