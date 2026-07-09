"""Harness smoke: prove the builder + real-git policy work before the matrix."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration


def test_install_deploys_against_real_git_source(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env()
    result = env.run_verb(["install"])
    assert result.exit_code == 0, result.output
    live = env.live(".setforge_it/text/note.txt")
    assert live.read_text() == "hello from tracked\n"


def test_plain_path_source_skips_git(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = integration_env(git_init=False)
    result = env.run_verb(["install"])
    assert result.exit_code == 0, result.output
    assert env.live(".setforge_it/json/settings.json").exists()
