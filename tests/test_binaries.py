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
