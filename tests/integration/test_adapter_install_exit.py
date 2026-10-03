"""Install exit status when extension reconciliation fails."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration

_INSTALL = ["install", "--yes", "--no-git-check", "--no-secrets-scan"]


def _declare_extension(env: IntegrationEnv) -> None:
    result = env.run_verb(["ext", "add", "ms.python", "--no-install"])
    assert result.exit_code == 0, result.output


def test_failed_extension_install_exits_nonzero(
    integration_env: Callable[..., IntegrationEnv], integration_subprocess
) -> None:
    env = integration_env()
    _declare_extension(env)
    code = str(env.present_binary("code"))
    integration_subprocess.register(
        [code, "--list-extensions"], stdout="other.ext\n", occurrences=5
    )
    integration_subprocess.register(
        [code, "--install-extension", integration_subprocess.any()],
        returncode=5,
        stderr="network down",
        occurrences=5,
    )

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 1, result.output
    assert "ms.python" in result.output
    assert env.live(".setforge_it/text/note.txt").exists()


def test_missing_code_binary_is_skipped_with_exit_zero(
    integration_env: Callable[..., IntegrationEnv], integration_subprocess
) -> None:
    env = integration_env()
    _declare_extension(env)

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 0, result.output
