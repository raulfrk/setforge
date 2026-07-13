"""Real-TTY Docker e2e for the first-install seed divergence-preview pager."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-minimal"
_TRACKED = "/workspace/tests/fixtures/e2e/tracked/minimal/text.txt"
_LIVE = "/home/tester/.setforge_e2e/minimal/text.txt"

_LIVE_BODY = "".join(f"live-line-{i}\n" for i in range(60))
_UPSTREAM_BODY = "".join(f"upstream-line-{i}\n" for i in range(60))

_FRAME_GLYPH = "┌"


def _seed_session(
    pyte_pty_session: Callable[..., PyteSession],
    c: ContainerHandle,
    *,
    cols: int = 120,
    lines: int = 40,
) -> PyteSession:
    return pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--reconcile-user-sections",
            "--no-secrets-scan",
            "--no-transition",
            "--no-git-check",
        ],
        cols=cols,
        lines=lines,
        timeout=120.0,
    )


def _seed_divergence(c: ContainerHandle) -> None:
    c.write_text(_LIVE, _LIVE_BODY)
    c.write_text(_TRACKED, _UPSTREAM_BODY)


@pytest.mark.xdist_group("docker_daemon")
def test_seed_pager_view_scroll_resize_then_choose(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _seed_divergence(c)

    s = _seed_session(pyte_pty_session, c, cols=120, lines=40)
    s.expect_in_display(_FRAME_GLYPH)
    s.expect_in_display("live vs upstream")
    s.expect_in_display("View diff")

    s.send_keys("v")
    s.expect_in_display("-live-line-0")
    s.send_keys("G")
    s.expect_in_display("+upstream-line-59")
    s.resize(cols=100, lines=12)
    s.send_keys("G")
    s.expect_in_display("+upstream-line-59")

    s.send_keys("q")
    s.resize(cols=120, lines=40)
    s.expect_in_display("View diff")
    s.send_keys("t")
    s.wait_for_exit(timeout=60, expected_code=0)

    assert c.read_text(_LIVE) == _UPSTREAM_BODY


@pytest.mark.xdist_group("docker_daemon")
def test_seed_pager_escape_aborts_file_unchanged(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _seed_divergence(c)

    s = _seed_session(pyte_pty_session, c)
    s.expect_in_display(_FRAME_GLYPH)
    s.expect_in_display("View diff")
    s.send_keys("v")
    s.expect_in_display("-live-line-0")
    s.send_keys("\x1b")
    s.wait_for_exit(timeout=60, expected_code=0)

    assert c.read_text(_LIVE) == _LIVE_BODY
    s2 = _seed_session(pyte_pty_session, c)
    s2.expect_in_display("live vs upstream")
    s2.send_keys("\r")
    s2.wait_for_exit(timeout=60, expected_code=0)
    assert c.read_text(_LIVE) == _LIVE_BODY
