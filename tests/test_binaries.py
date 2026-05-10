"""Tests for my_setup.binaries — host-local binary override resolver."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from my_setup import binaries
from my_setup.errors import BinaryOverrideInvalid, ConfigError


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    """Redirect LOCAL_CONFIG_PATH into tmp_path and clear CLI/env state.

    Every test gets an isolated home so production state is never
    touched. Env vars for overrides are unset so a polluted shell can't
    leak into tests.
    """
    monkeypatch.setattr(binaries, "LOCAL_CONFIG_PATH", tmp_path / "local.yaml")
    binaries._cli_overrides.clear()
    for name in binaries.SUPPORTED_BINARIES:
        monkeypatch.delenv(
            f"{binaries._ENV_VAR_PREFIX}{name.upper()}{binaries._ENV_VAR_SUFFIX}",
            raising=False,
        )


def test_load_local_config_missing_returns_empty() -> None:
    assert binaries._load_local_config() == {}


def test_load_local_config_empty_file_returns_empty() -> None:
    binaries.LOCAL_CONFIG_PATH.write_text("")
    assert binaries._load_local_config() == {}


def test_load_local_config_no_binaries_key_returns_empty() -> None:
    binaries.LOCAL_CONFIG_PATH.write_text("other: true\n")
    assert binaries._load_local_config() == {}


def test_load_local_config_returns_binaries_mapping() -> None:
    binaries.LOCAL_CONFIG_PATH.write_text(
        "binaries:\n  code: /custom/code\n  patch: /custom/patch\n"
    )
    assert binaries._load_local_config() == {
        "code": "/custom/code",
        "patch": "/custom/patch",
    }


def test_load_local_config_malformed_yaml_raises() -> None:
    binaries.LOCAL_CONFIG_PATH.write_text("binaries:\n  code: [unterminated\n")
    with pytest.raises(ConfigError, match="malformed YAML"):
        binaries._load_local_config()


def test_load_local_config_binaries_not_a_mapping_raises() -> None:
    binaries.LOCAL_CONFIG_PATH.write_text("binaries: a-string\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        binaries._load_local_config()


def test_load_local_config_top_level_not_a_mapping_raises() -> None:
    binaries.LOCAL_CONFIG_PATH.write_text("- list\n- only\n")
    with pytest.raises(ConfigError, match="top-level"):
        binaries._load_local_config()


def test_env_overrides_none_set_returns_empty() -> None:
    assert binaries._env_overrides() == {}


def test_env_overrides_one_set(monkeypatch) -> None:
    monkeypatch.setenv("MY_SETUP_CODE_BIN", "/env/code")
    assert binaries._env_overrides() == {"code": "/env/code"}


def test_env_overrides_all_three_set(monkeypatch) -> None:
    monkeypatch.setenv("MY_SETUP_CODE_BIN", "/env/code")
    monkeypatch.setenv("MY_SETUP_CLAUDE_BIN", "/env/claude")
    monkeypatch.setenv("MY_SETUP_PATCH_BIN", "/env/patch")
    assert binaries._env_overrides() == {
        "code": "/env/code",
        "claude": "/env/claude",
        "patch": "/env/patch",
    }


def test_env_overrides_empty_string_treated_as_unset(monkeypatch) -> None:
    monkeypatch.setenv("MY_SETUP_CODE_BIN", "")
    assert binaries._env_overrides() == {}


def test_set_cli_overrides_stores_provided_values() -> None:
    binaries.set_cli_overrides(code="/cli/code", patch="/cli/patch")
    assert binaries._cli_overrides == {
        "code": "/cli/code",
        "patch": "/cli/patch",
    }


def test_set_cli_overrides_none_skipped() -> None:
    binaries.set_cli_overrides(code=None, claude=None, patch=None)
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
    binaries.LOCAL_CONFIG_PATH.write_text(f"binaries:\n  code: {bin_path}\n")
    assert binaries.resolve_binary("code") == bin_path


def test_resolve_env_overrides_config(monkeypatch, tmp_path) -> None:
    cfg_bin = _make_executable(tmp_path / "cfg-code")
    env_bin = _make_executable(tmp_path / "env-code")
    binaries.LOCAL_CONFIG_PATH.write_text(f"binaries:\n  code: {cfg_bin}\n")
    monkeypatch.setenv("MY_SETUP_CODE_BIN", str(env_bin))
    assert binaries.resolve_binary("code") == env_bin


def test_resolve_cli_overrides_env_and_config(monkeypatch, tmp_path) -> None:
    cfg_bin = _make_executable(tmp_path / "cfg-code")
    env_bin = _make_executable(tmp_path / "env-code")
    cli_bin = _make_executable(tmp_path / "cli-code")
    binaries.LOCAL_CONFIG_PATH.write_text(f"binaries:\n  code: {cfg_bin}\n")
    monkeypatch.setenv("MY_SETUP_CODE_BIN", str(env_bin))
    binaries.set_cli_overrides(code=str(cli_bin))
    assert binaries.resolve_binary("code") == cli_bin


def test_resolve_invalid_cli_override_raises(tmp_path) -> None:
    binaries.set_cli_overrides(code=str(tmp_path / "nope"))
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries.resolve_binary("code")
    assert excinfo.value.layer == "cli"


def test_resolve_invalid_env_override_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MY_SETUP_CODE_BIN", str(tmp_path / "nope"))
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries.resolve_binary("code")
    assert excinfo.value.layer == "env"


def test_resolve_invalid_config_override_raises(tmp_path) -> None:
    binaries.LOCAL_CONFIG_PATH.write_text(f"binaries:\n  code: {tmp_path / 'nope'}\n")
    with pytest.raises(BinaryOverrideInvalid) as excinfo:
        binaries.resolve_binary("code")
    assert excinfo.value.layer == "config"


def test_ensure_stub_creates_file_when_absent() -> None:
    assert not binaries.LOCAL_CONFIG_PATH.exists()
    binaries.ensure_local_config_stub()
    assert binaries.LOCAL_CONFIG_PATH.exists()
    text = binaries.LOCAL_CONFIG_PATH.read_text(encoding="utf-8")
    assert "binaries:" in text
    assert text.startswith("# my-setup host-local config")


def test_ensure_stub_creates_parent_directories(monkeypatch, tmp_path) -> None:
    nested = tmp_path / "deep" / "nested" / "local.yaml"
    monkeypatch.setattr(binaries, "LOCAL_CONFIG_PATH", nested)
    binaries.ensure_local_config_stub()
    assert nested.exists()


def test_ensure_stub_does_not_overwrite_existing() -> None:
    binaries.LOCAL_CONFIG_PATH.write_text("user content\n")
    binaries.ensure_local_config_stub()
    assert binaries.LOCAL_CONFIG_PATH.read_text() == "user content\n"


def test_ensure_stub_is_idempotent() -> None:
    binaries.ensure_local_config_stub()
    first_mtime = binaries.LOCAL_CONFIG_PATH.stat().st_mtime_ns
    binaries.ensure_local_config_stub()
    assert binaries.LOCAL_CONFIG_PATH.stat().st_mtime_ns == first_mtime


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
