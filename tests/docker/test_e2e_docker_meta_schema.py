"""One real-process transition lifecycle at the Docker boundary.

Transition serialization, backward compatibility, redaction, and individual
command branches are covered by the unit and integration tiers.  Docker owns
only the observable boundary those tiers cannot provide: three installed CLI
processes sharing a fresh host's persisted transition directory.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_TRANSITIONS_DIR = "/home/tester/.local/state/setforge/transitions"
_LIVE = "/home/tester/.setforge_e2e/disposition/shared.md"


def _latest_meta(container: ContainerHandle) -> dict[str, object]:
    latest = container.exec(
        ["bash", "-c", f"ls -1 {_TRANSITIONS_DIR} | sort | tail -1"]
    ).stdout.strip()
    assert latest, "no transition recorded in container state dir"
    return json.loads(container.read_text(f"{_TRANSITIONS_DIR}/{latest}/meta.json"))


def _assert_current_meta(container: ContainerHandle, command: str) -> None:
    meta = _latest_meta(container)
    assert meta["command"] == command
    assert isinstance(meta.get("end_timestamp"), str)
    command_line = meta.get("command_line")
    assert isinstance(command_line, list)
    assert command in (str(arg) for arg in command_line)
    assert "preserve_user_keys_applied" not in meta


@pytest.mark.smoke
@pytest.mark.xdist_group("docker_daemon")
def test_transition_metadata_persists_across_cli_lifecycle(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Install, sync, and revert processes share valid transition metadata."""
    container = docker_container()

    install = container.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-disposition-shared",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    assert install.returncode == 0, install.stderr
    _assert_current_meta(container, "install")

    container.write_text(
        _LIVE,
        "# Disposition fixture\n\nintro line\nmiddle-CAPTURED\nfooter line\n",
    )
    sync = container.exec(
        [
            "uv",
            "run",
            "setforge",
            "sync",
            "--profile=test-disposition-shared",
            f"--config={CONFIG_FIXTURE}",
            "--auto=use-live",
            "--yes",
        ],
        check=False,
    )
    assert sync.returncode == 0, sync.stderr
    _assert_current_meta(container, "sync")

    revert = container.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-disposition-shared",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ],
        check=False,
    )
    assert revert.returncode == 0, revert.stderr
    _assert_current_meta(container, "revert")
