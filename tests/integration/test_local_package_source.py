"""Local packages resolve their tracked file from the manifest's own repo."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration


def _declare_local_package(env: IntegrationEnv, install_dir: Path) -> None:
    (env.repo / "tracked" / "mytool").write_text("#!/bin/sh\necho hi\n")
    text = env.config.read_text()
    text = text.replace(
        "profiles:\n",
        "packages:\n  mytool:\n    type: local\n    path: mytool\n"
        f"    binary: mytool\n    install: {install_dir}\n    extract: false\n"
        "profiles:\n",
        1,
    )
    text = text.rstrip("\n") + "\n    packages: [mytool]\n"
    env.config.write_text(text)


def test_install_from_other_cwd_provisions_local_package(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = integration_env()
    install_dir = tmp_path / "bin"
    _declare_local_package(env, install_dir)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("SETFORGE_SOURCE", raising=False)

    result = env.run_verb(["install", "--yes", "--no-git-check", "--no-secrets-scan"])

    assert result.exit_code == 0, result.output
    assert (install_dir / "mytool").read_text() == "#!/bin/sh\necho hi\n"
    assert not (env.state_dir / "operations").exists() or not any(
        (env.state_dir / "operations").rglob("*.json")
    )
