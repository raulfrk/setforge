"""Tests for my_setup.binaries — host-local binary override resolver."""
from __future__ import annotations

import stat
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
    monkeypatch.setattr(
        binaries, "LOCAL_CONFIG_PATH", tmp_path / "local.yaml"
    )
    binaries._cli_overrides.clear()
    for name in binaries.SUPPORTED_BINARIES:
        monkeypatch.delenv(
            f"{binaries._ENV_VAR_PREFIX}{name.upper()}{binaries._ENV_VAR_SUFFIX}",
            raising=False,
        )


def test_load_local_config_missing_returns_empty():
    assert binaries._load_local_config() == {}


def test_load_local_config_empty_file_returns_empty():
    binaries.LOCAL_CONFIG_PATH.write_text("")
    assert binaries._load_local_config() == {}


def test_load_local_config_no_binaries_key_returns_empty():
    binaries.LOCAL_CONFIG_PATH.write_text("other: true\n")
    assert binaries._load_local_config() == {}


def test_load_local_config_returns_binaries_mapping():
    binaries.LOCAL_CONFIG_PATH.write_text(
        "binaries:\n  code: /custom/code\n  patch: /custom/patch\n"
    )
    assert binaries._load_local_config() == {
        "code": "/custom/code",
        "patch": "/custom/patch",
    }


def test_load_local_config_malformed_yaml_raises():
    binaries.LOCAL_CONFIG_PATH.write_text("binaries:\n  code: [unterminated\n")
    with pytest.raises(ConfigError, match="malformed YAML"):
        binaries._load_local_config()


def test_load_local_config_binaries_not_a_mapping_raises():
    binaries.LOCAL_CONFIG_PATH.write_text("binaries: a-string\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        binaries._load_local_config()


def test_load_local_config_top_level_not_a_mapping_raises():
    binaries.LOCAL_CONFIG_PATH.write_text("- list\n- only\n")
    with pytest.raises(ConfigError, match="top-level"):
        binaries._load_local_config()


def test_env_overrides_none_set_returns_empty():
    assert binaries._env_overrides() == {}


def test_env_overrides_one_set(monkeypatch):
    monkeypatch.setenv("MY_SETUP_CODE_BIN", "/env/code")
    assert binaries._env_overrides() == {"code": "/env/code"}


def test_env_overrides_all_three_set(monkeypatch):
    monkeypatch.setenv("MY_SETUP_CODE_BIN", "/env/code")
    monkeypatch.setenv("MY_SETUP_CLAUDE_BIN", "/env/claude")
    monkeypatch.setenv("MY_SETUP_PATCH_BIN", "/env/patch")
    assert binaries._env_overrides() == {
        "code": "/env/code",
        "claude": "/env/claude",
        "patch": "/env/patch",
    }


def test_env_overrides_empty_string_treated_as_unset(monkeypatch):
    monkeypatch.setenv("MY_SETUP_CODE_BIN", "")
    assert binaries._env_overrides() == {}


def test_set_cli_overrides_stores_provided_values():
    binaries.set_cli_overrides(code="/cli/code", patch="/cli/patch")
    assert binaries._cli_overrides == {
        "code": "/cli/code",
        "patch": "/cli/patch",
    }


def test_set_cli_overrides_none_skipped():
    binaries.set_cli_overrides(code=None, claude=None, patch=None)
    assert binaries._cli_overrides == {}


def test_set_cli_overrides_replaces_prior():
    binaries.set_cli_overrides(code="/first")
    binaries.set_cli_overrides(claude="/second")
    assert binaries._cli_overrides == {"claude": "/second"}
