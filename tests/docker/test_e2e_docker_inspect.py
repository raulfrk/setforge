"""Docker e2e for the A7 ``setforge inspect`` 3-way viewer."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-stage"
_LIVE = "/home/tester/.setforge_e2e/stage/notes.md"

_LIVE_BODY = (
    "# Shareable notes\n\n"
    "## Tooling\n"
    "Use rg not grep; fd not find.\n\n"
    "## Shell\n"
    "Prefer zsh over bash.\n\n"
    "## Host paths\n"
    "workdir: /home/tester\n"
)


def _setforge(c: ContainerHandle, args: list[str]) -> tuple[int, str, str]:
    result = c.exec(["uv", "run", "setforge", *args], check=False)
    return result.returncode, result.stdout, result.stderr


def _install(c: ContainerHandle) -> tuple[int, str, str]:
    return _setforge(
        c, ["install", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"]
    )


def _inspect_session(
    pyte_pty_session: Callable[..., PyteSession], c: ContainerHandle, *, cols: int
) -> PyteSession:
    return pyte_pty_session(
        container=c.cid,
        cols=cols,
        cmd=[
            "uv",
            "run",
            "setforge",
            "inspect",
            "stage_notes",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
        timeout=120.0,
    )


@pytest.mark.xdist_group("docker_daemon")
def test_inspect_wide_pty_renders_three_pane_panel(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    assert _install(c)[0] == 0
    c.write_text(_LIVE, _LIVE_BODY)

    s = _inspect_session(pyte_pty_session, c, cols=160)
    s.expect_in_display("base | live | merge", timeout=60.0)
    s.expect_in_display("Prefer zsh over bash.", timeout=30.0)
    s.wait_for_exit(timeout=60, expected_code=0)


@pytest.mark.xdist_group("docker_daemon")
def test_inspect_narrow_pty_stacks(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    assert _install(c)[0] == 0
    c.write_text(_LIVE, _LIVE_BODY)

    s = _inspect_session(pyte_pty_session, c, cols=80)
    s.expect_in_display("base | live | merge", timeout=60.0)
    s.expect_in_display("Prefer zsh over bash.", timeout=30.0)
    s.wait_for_exit(timeout=60, expected_code=0)


@pytest.mark.xdist_group("docker_daemon")
def test_inspect_json_envelope_ansi_free_in_pipe(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    assert _install(c)[0] == 0
    c.write_text(_LIVE, _LIVE_BODY)

    rc, out, err = _setforge(
        c,
        [
            "-o",
            "json",
            "inspect",
            "stage_notes",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, err
    assert "\x1b[" not in out
    envelope = json.loads(out)
    assert envelope["command"] == "inspect"
    data = envelope["data"]
    assert data["base_present"] is True
    assert set(data["panes"]) == {"base", "live", "merge"}
    assert "Prefer zsh over bash." in data["panes"]["live"]
    assert set(data["index"]) == {"shared", "kept_local", "conflict"}
