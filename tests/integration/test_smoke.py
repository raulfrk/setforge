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
    """install on a real-git source repo lands the tracked bytes live.

    The source is a real git checkout, so the source-clean gate runs the
    REAL ``git status --porcelain`` (finds it clean) — the integration
    seam this tier exists to exercise. No claude/code/gitleaks is
    registered; none is on PATH in the sandbox, so the reconcile legs
    warn-and-skip. Observable = exit code + on-disk live body.
    """
    env = integration_env()
    result = env.run_verb(["install"])
    assert result.exit_code == 0, result.output
    live = env.live(".setforge_it/text/note.txt")
    assert live.read_text() == "hello from tracked\n"


def test_plain_path_source_skips_git(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    """A non-git PathSource install runs zero git subprocesses.

    ``git_init=False`` leaves no ``.git``; the source-clean gate
    short-circuits on ``is_git_repo`` (a cheap filesystem check, no
    shell-out). Proves the harness handles both source shapes.
    """
    env = integration_env(git_init=False)
    result = env.run_verb(["install"])
    assert result.exit_code == 0, result.output
    assert env.live(".setforge_it/json/settings.json").exists()
