"""Docker E2E: bundle ``file`` component, real chmod + reinstall round-trip."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker


_WORKDIR = "/home/tester/bundle-file-e2e"
_CFG = f"{_WORKDIR}/setforge.yaml"
_SRC = f"{_WORKDIR}/tracked/launch.sh"
_DST = "/home/tester/.claude/plugins/data/revdiff/scripts/launch.sh"

_LAUNCHER_BODY = "#!/bin/sh\necho revdiff launcher\n"

_CFG_TEXT = (
    "version: 1\n"
    "tracked_files: {}\n"
    "bundles:\n"
    "  revdiff:\n"
    "    components:\n"
    "      - id: launcher\n"
    "        file:\n"
    "          src: launch.sh\n"
    f"          dst: {_DST}\n"
    "          mode: 0o755\n"
    "profiles:\n"
    "  test-bundle-file:\n"
    "    bundles:\n"
    "      - revdiff\n"
)


def _bootstrap(c: ContainerHandle) -> None:
    c.exec(["mkdir", "-p", f"{_WORKDIR}/tracked"], check=True)
    c.write_text(_CFG, _CFG_TEXT)
    c.write_text(_SRC, _LAUNCHER_BODY)


def _setforge(
    c: ContainerHandle,
    args: list[str],
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return c.exec(["uv", "run", "setforge", *args], check=check)


def _install(c: ContainerHandle) -> subprocess.CompletedProcess[str]:
    return _setforge(
        c, ["install", "--profile=test-bundle-file", f"--config={_CFG}"], check=False
    )


def test_bundle_file_e2e_deploys_executable(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _bootstrap(c)

    result = _install(c)
    assert result.returncode == 0, result.stdout + result.stderr

    assert c.exec(["test", "-f", _DST], check=False).returncode == 0, "dst missing"
    assert c.exec(["cat", _DST], check=True).stdout == _LAUNCHER_BODY

    assert c.exec(["test", "-x", _DST], check=False).returncode == 0, (
        "launcher is not executable"
    )
    perms = c.exec(["stat", "-c", "%a", _DST], check=True).stdout.strip()
    assert perms == "755", f"expected mode 755, got {perms}"


def test_bundle_file_e2e_hand_edit_survives_reinstall(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _bootstrap(c)

    first = _install(c)
    assert first.returncode == 0, first.stdout + first.stderr

    edited_body = "#!/bin/sh\necho EDITED launcher\n"
    c.write_text(_DST, edited_body)

    second = _install(c)
    assert second.returncode == 0, second.stdout + second.stderr

    survived = c.exec(["cat", _DST], check=True).stdout
    assert "EDITED" in survived, f"hand-edit clobbered by re-install: {survived!r}"
