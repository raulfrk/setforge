"""Docker e2e: two ``disposition: shared`` markdown files coexist in one config.

Proves end-to-end that a single profile carrying two shared-disposition
markdown files — one whose tracked src carries a shared user-section, one
plain — installs in one run with each file's behavior intact and no
cross-file interference: the run-global keep-set prune retains each
disposition base, and the distinct ``dst`` paths keep the per-host bases from
ever crossing. Under the 2.0 contract both files reconcile through the same
shared 3-way merge (there is no longer a separate file-preservation model),
so a live edit to one file's shared section and a live edit to the other's
footer must both survive a re-install — the proof the two bases ran
independently.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-dual-representation"
_LIVE_HOST_LOCAL = "/home/tester/.setforge_e2e/host-local/host.md"
_LIVE_DISPOSITION = "/home/tester/.setforge_e2e/disposition/shared.md"
_BASE_DIR = "/home/tester/.local/state/setforge/base/test-dual-representation"
_DISPOSITION_BASE = f"{_BASE_DIR}/disposition_shared_md"
_HOST_LOCAL_BASE = f"{_BASE_DIR}/host_local_md"


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _install(c: ContainerHandle) -> tuple[int, str, str]:
    """Run ``setforge install`` for the dual-representation profile."""
    return _setforge(
        c, ["install", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"]
    )


@pytest.mark.xdist_group("docker_daemon")
def test_dual_representation_single_install(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Both shared files install in one run; their bases stay independent.

    First install: each file seeds its own stored base from tracked and lands
    its content. Then edit the live shared user-section in one file AND the
    live footer in the other (disjoint hunks → clean 3-way each) and
    re-install: the shared-section edit survives and the footer edit survives —
    proving the two files reconciled through independent per-host bases in the
    same install. Both bases existing after the first run proves the run-global
    keep-set prune did not drop either.
    """
    c = docker_container()

    rc, _out, err = _install(c)
    assert rc == 0, err
    # Independence guard: the run-global keep-set prune retained BOTH bases.
    assert c.read_text(_DISPOSITION_BASE), "disposition base missing"
    assert c.read_text(_HOST_LOCAL_BASE), "host-local base missing"
    # The shared-section file deployed with its user-section markers intact.
    host_live = c.read_text(_LIVE_HOST_LOCAL)
    assert "setforge:user-section start shared notes" in host_live, host_live

    # Edit the live shared user-section body in one file and the live
    # disposition footer in the other (disjoint hunks), then re-install.
    edited = host_live.replace("default notes (tracked side)", "MY HOST EDIT")
    assert edited != host_live, host_live
    c.write_text(_LIVE_HOST_LOCAL, edited)
    c.write_text(
        _LIVE_DISPOSITION,
        "# Disposition fixture\n\nintro line\nmiddle line\nfooter-LIVE\n",
    )
    rc2, _out2, err2 = _install(c)
    assert rc2 == 0, err2

    # First file's shared 3-way merge kept the live section edit.
    assert "MY HOST EDIT" in c.read_text(_LIVE_HOST_LOCAL)
    # Second file's shared 3-way merge kept the live footer.
    assert "footer-LIVE" in c.read_text(_LIVE_DISPOSITION)

    # compare sees BOTH files in one profile without error.
    rc3, stdout, err3 = _setforge(
        c,
        [
            "-o",
            "json",
            "compare",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc3 == 0, err3
    entries = {e["name"] for e in json.loads(stdout)["data"]["entries"]}
    assert {"host_local_md", "disposition_shared_md"} <= entries, entries
