"""A local package never overwrites a file it did not install."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from .conftest import IntegrationEnv
from .test_local_package_source import _declare_local_package

pytestmark = pytest.mark.integration


def test_install_leaves_preexisting_user_file_in_place(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess: object,
    tmp_path: Path,
) -> None:
    env = integration_env()
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    mine = install_dir / "mytool"
    mine.write_text("my own binary\n")
    _declare_local_package(env, install_dir)

    result = env.run_verb(["install", "--yes", "--no-git-check", "--no-secrets-scan"])

    assert mine.read_text() == "my own binary\n", result.output
