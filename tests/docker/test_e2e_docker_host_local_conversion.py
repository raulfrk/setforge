"""Docker e2e: tracked-authored host-local marker → markerless overlay (14.17).

Increment 3 of the host-local de-marker conversion, exercised end-to-end against
a fresh container with the real ``setforge`` CLI. Distinct from
``test_e2e_docker_host_local.py`` (which drives the local.yaml
``host_local_sections`` INJECTION path): here the host-local marker pair is
authored in the TRACKED source (``sections/marked.md``, the ``sections_md`` /
``test-text-sections`` fixture) and carries a per-host body in the deployed live
file. The first install must capture that body into a local.yaml at-end-of-file
overlay span and render the deployed file markerless — without losing the body
(deploy's blanket strip would otherwise delete it).

Cases: first-install conversion, per-host edit survives, idempotent re-install,
capture-no-leak on sync, revert restores the markers.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-text-sections"
_LIVE = "/home/tester/.setforge_e2e/sections/marked.md"
_TRACKED = "/workspace/tests/fixtures/e2e/tracked/sections/marked.md"
_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_H = "f" * 64

# A deployed live file carrying the tracked-authored host-local marker pair with
# a per-host edit inside it (the pre-14.17 steady state: preserve_user_sections
# kept the body across installs). The body must survive conversion.
_LIVE_PRESEED = (
    "# test-text-sections fixture\n\n"
    "Global tracked text overwritten on every install.\n\n"
    "<!-- setforge:user-section start host-local notes -->\n"
    "MY PER-HOST NOTES\n"
    f"<!-- setforge:user-section end host-local notes hash={_H} -->\n\n"
    "Trailing tracked content.\n"
)


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _install(c: ContainerHandle, *, check: bool = False) -> tuple[int, str, str]:
    return _setforge(
        c,
        ["install", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"],
        check=check,
    )


def test_first_install_converts_tracked_host_local_to_markerless(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """One install captures the live host-local body and renders the file markerless."""
    c = docker_container()
    c.write_text(_LIVE, _LIVE_PRESEED)  # pre-deployed markered per-host body

    rc, _stdout, stderr = _install(c)
    assert rc == 0, stderr

    live = c.read_text(_LIVE)
    assert "setforge:user-section" not in live  # zero markers after one install
    assert live.count("MY PER-HOST NOTES") == 1  # per-host body survived, once
    # Captured into a markerless overlay span in local.yaml.
    local = c.read_text(_LOCAL_YAML)
    assert "kind: overlay" in local
    assert "MY PER-HOST NOTES" in local

    # Re-install is a no-op on the (now markerless) live file.
    rc2, _o2, e2 = _install(c)
    assert rc2 == 0, e2
    assert c.read_text(_LIVE) == live


def test_conversion_sync_does_not_leak_per_host_body(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """After conversion, sync must not bake the per-host body into the tracked src."""
    c = docker_container()
    c.write_text(_LIVE, _LIVE_PRESEED)
    _install(c, check=True)

    rc, _stdout, stderr = _setforge(
        c, ["sync", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}", "-y"]
    )
    assert rc == 0, stderr
    tracked = c.read_text(_TRACKED)
    assert "MY PER-HOST NOTES" not in tracked  # leak gate holds


def test_conversion_revert_restores_host_local_markers(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Revert restores the host-local markers in the live file."""
    c = docker_container()
    c.write_text(_LIVE, _LIVE_PRESEED)
    _install(c, check=True)
    assert "setforge:user-section" not in c.read_text(_LIVE)  # converted

    rc, _stdout, stderr = _setforge(
        c, ["revert", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}", "-y"]
    )
    assert rc == 0, stderr
    live = c.read_text(_LIVE)
    assert "start host-local notes" in live  # markers restored
    assert "MY PER-HOST NOTES" in live  # body restored inside the markers
