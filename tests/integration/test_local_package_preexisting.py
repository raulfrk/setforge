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


def _install(env: IntegrationEnv):
    return env.run_verb(["install", "--yes", "--no-git-check", "--no-secrets-scan"])


def test_install_adopts_identical_preexisting_file_without_receipt(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess: object,
    tmp_path: Path,
) -> None:
    env = integration_env()
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    existing = install_dir / "mytool"
    existing.write_text("#!/bin/sh\necho hi\n")
    _declare_local_package(env, install_dir)

    result = _install(env)

    assert result.exit_code == 0, result.output
    assert existing.read_text() == "#!/bin/sh\necho hi\n"
    receipts = list(env.state_dir.rglob("*"))
    assert any(
        str(existing.resolve()) in p.read_text() for p in receipts if p.is_file()
    )


def test_install_refuses_symlink_to_identical_file(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess: object,
    tmp_path: Path,
) -> None:
    env = integration_env()
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    real = tmp_path / "real"
    real.write_text("#!/bin/sh\necho hi\n")
    link = install_dir / "mytool"
    link.symlink_to(real)
    _declare_local_package(env, install_dir)

    result = _install(env)

    assert link.is_symlink(), result.output
    assert result.exit_code != 0
