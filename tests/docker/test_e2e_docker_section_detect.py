"""Docker E2E tests for ``setforge section detect`` (S4 carve wizard + S5
overlay re-capture).

Run in real Debian containers with the actual installed ``setforge`` CLI; the
interactive carve wizard is driven through the pyte PTY harness. Per the
approved plan these isolate each KIND in its own detect run (the two-file
scenario):

- OVERLAY carve on ``comprehensive_text`` (a clean ``disposition: None``
  markdown file with no legacy host-local markers).
- PINNED carve on ``host_local_md`` (``disposition: shared``).

Each round-trips through ``install`` and re-runs ``detect`` to prove idempotency;
a separate abort case proves the carve is atomic (Ctrl-C leaves no span).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_SOURCE_BLOCK = "source:\n  kind: path\n  path: /workspace/tests/fixtures/e2e\n"

# OVERLAY target — a clean disposition=None markdown file (no legacy markers).
_OVERLAY_PROFILE = "test-comprehensive"
_OVERLAY_TF = "comprehensive_text"
_OVERLAY_LIVE = "/home/tester/.setforge_e2e/comprehensive/notes.md"
_OVERLAY_TRACKED = "/workspace/tests/fixtures/e2e/tracked/comprehensive/notes.md"

# PINNED target — a disposition=shared file.
_PINNED_PROFILE = "test-host-local"
_PINNED_TF = "host_local_md"
_PINNED_LIVE = "/home/tester/.setforge_e2e/host-local/host.md"
_PINNED_TRACKED = "/workspace/tests/fixtures/e2e/tracked/host-local/host.md"


def _setforge(c: ContainerHandle, args: list[str]) -> tuple[int, str, str]:
    r = c.exec(["uv", "run", "setforge", *args], check=False)
    return r.returncode, r.stdout, r.stderr


def _install(
    c: ContainerHandle, profile: str, *, extra: list[str] | None = None
) -> None:
    args = ["install", f"--profile={profile}", f"--config={CONFIG_FIXTURE}"]
    if extra:
        args.extend(extra)
    rc, out, err = _setforge(c, args)
    assert rc == 0, err or out


def _detect(profile: str, tracked_file: str) -> list[str]:
    return [
        "section",
        "detect",
        f"--profile={profile}",
        f"--config={CONFIG_FIXTURE}",
        f"--tracked-file={tracked_file}",
    ]


def _detect_cmd(profile: str, tracked_file: str) -> list[str]:
    return ["uv", "run", "setforge", *_detect(profile, tracked_file)]


@pytest.mark.xdist_group("docker_daemon")
def test_detect_overlay_carve_roundtrip(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """Edit live → detect → carve overlay → install → live kept, tracked clean,
    local.yaml carries the span; re-detect finds nothing (idempotency)."""
    c = docker_container()
    c.write_text(_LOCAL_YAML, _SOURCE_BLOCK)
    _install(c, _OVERLAY_PROFILE)

    tracked_before = c.read_text(_OVERLAY_TRACKED)
    live0 = c.read_text(_OVERLAY_LIVE)
    # One new host-only line → a NEW_CONTENT region → overlay carve.
    c.write_text(_OVERLAY_LIVE, live0 + "MY HOST NOTE alpha\n")

    session = pyte_pty_session(
        container=c.cid, cmd=_detect_cmd(_OVERLAY_PROFILE, _OVERLAY_TF), timeout=60.0
    )
    session.expect_in_display("[carve/extend/skip]", timeout=30.0)
    session.send_keys("carve\r")
    session.expect_in_display("name:", timeout=10.0)
    session.send_keys("vmnotes\r")
    session.expect_in_display("scope", timeout=10.0)
    session.send_keys("host-local\r")
    session.expect_in_display("wrote 1 span", timeout=20.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)

    # tracked src untouched (no host-local leak into the config repo).
    assert c.read_text(_OVERLAY_TRACKED) == tracked_before
    # local.yaml carries the overlay span.
    local_yaml = c.read_text(_LOCAL_YAML)
    assert "vmnotes" in local_yaml
    assert "overlay" in local_yaml
    # live still carries the host note.
    assert "MY HOST NOTE alpha" in c.read_text(_OVERLAY_LIVE)

    # install re-injects the overlay; re-detect is idempotent.
    _install(c, _OVERLAY_PROFILE, extra=["--auto=use-tracked", "--yes"])
    assert "MY HOST NOTE alpha" in c.read_text(_OVERLAY_LIVE)
    rc, out, err = _setforge(c, _detect(_OVERLAY_PROFILE, _OVERLAY_TF))
    assert rc == 0, err or out
    assert "no changes detected" in (out + err)


@pytest.mark.xdist_group("docker_daemon")
def test_detect_pinned_carve_roundtrip(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """Diverge a tracked section live → detect → carve pinned → install keeps the
    divergence; tracked clean, local.yaml carries the pinned span; idempotent."""
    c = docker_container()
    c.write_text(_LOCAL_YAML, _SOURCE_BLOCK)
    _install(c, _PINNED_PROFILE)

    tracked_before = c.read_text(_PINNED_TRACKED)
    live0 = c.read_text(_PINNED_LIVE)
    assert "Workflow body content" in live0
    c.write_text(
        _PINNED_LIVE,
        live0.replace("Workflow body content", "Workflow body content MY HOST EDIT"),
    )

    session = pyte_pty_session(
        container=c.cid, cmd=_detect_cmd(_PINNED_PROFILE, _PINNED_TF), timeout=60.0
    )
    session.expect_in_display("[carve/extend/skip]", timeout=30.0)
    session.send_keys("carve\r")
    session.expect_in_display("name:", timeout=10.0)
    session.send_keys("workflow\r")
    session.expect_in_display("scope", timeout=10.0)
    session.send_keys("host-local\r")
    session.expect_in_display("kind", timeout=10.0)
    session.send_keys("pinned\r")
    session.expect_in_display("wrote 1 span", timeout=20.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)

    assert c.read_text(_PINNED_TRACKED) == tracked_before
    local_yaml = c.read_text(_LOCAL_YAML)
    assert "## Workflow" in local_yaml
    assert "pinned" in local_yaml
    assert "MY HOST EDIT" in c.read_text(_PINNED_LIVE)

    # install re-imposes the pinned divergence; re-detect is idempotent.
    _install(c, _PINNED_PROFILE, extra=["--auto=use-tracked", "--yes"])
    assert "MY HOST EDIT" in c.read_text(_PINNED_LIVE)
    rc, out, err = _setforge(c, _detect(_PINNED_PROFILE, _PINNED_TF))
    assert rc == 0, err or out
    assert "no changes detected" in (out + err)


@pytest.mark.xdist_group("docker_daemon")
def test_detect_abort_leaves_no_span(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """Ctrl-C mid-wizard leaves local.yaml byte-identical (no half-created span)."""
    c = docker_container()
    c.write_text(_LOCAL_YAML, _SOURCE_BLOCK)
    _install(c, _OVERLAY_PROFILE)
    live0 = c.read_text(_OVERLAY_LIVE)
    c.write_text(_OVERLAY_LIVE, live0 + "MY HOST NOTE beta\n")
    local_before = c.read_text(_LOCAL_YAML)

    session = pyte_pty_session(
        container=c.cid, cmd=_detect_cmd(_OVERLAY_PROFILE, _OVERLAY_TF), timeout=60.0
    )
    session.expect_in_display("[carve/extend/skip]", timeout=30.0)
    session.send_keys("carve\r")
    session.expect_in_display("name:", timeout=10.0)
    session.send_keys("\x03")  # Ctrl-C before the carve commits
    time.sleep(2.0)
    session.close()

    assert c.read_text(_LOCAL_YAML) == local_before  # no span written
