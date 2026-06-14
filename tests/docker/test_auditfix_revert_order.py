"""Docker e2e: a SECOND `revert` acts as a REDO of the install.

Audit coverage gap (``revert_order`` finding 2): ``revert`` records its
own reverse transition (file_pre snapshot + reverse_store_state recapture
+ reverse_modes recapture) so that running ``revert`` a second time
RE-APPLIES the install/sync the first revert undid — including the
per-host store state. The e2e suite already covers install→revert and
install→revert→reinstall
(``test_e2e_docker_state_snapshots.py::test_install_revert_reinstall_repeats_first_run_verbatim``)
but NEVER install→revert→revert-as-redo. A regression that mis-records
the reverse transition (wrong store recapture order, omitted snapshot)
would silently break redo end-to-end with no e2e gate.

This test extends the state-snapshot scenario to the redo leg: after a
first revert deletes the seeded stores and restores the live span edit, a
SECOND revert must re-apply the install verbatim — live clobbered again
and BOTH store entries re-seeded byte-identically to the first install.

NOT run here (no docker daemon); listed in e2eTestsWritten for the gated
``uv run pytest tests/docker/ -m e2e_docker`` suite.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Live destination + per-host derived state for the forked-span profile
# (mirrors test_e2e_docker_state_snapshots.py).
_LIVE_FORKED = "/home/tester/.setforge_e2e/spans/forked.md"
_BASE_FORKED = (
    "/home/tester/.local/state/setforge/base/test-spans-forked/spans_forked_md"
)
_SIDECAR_FORKED = (
    "/home/tester/.local/state/setforge/spans/test-spans-forked/spans_forked_md.json"
)

# The canonical tracked body (matches the fixture src on disk).
_TRACKED_MD_BODY = (
    "# Spans fixture\n\n"
    "## Upstream\n"
    "upstream line A\n"
    "upstream line B\n\n"
    "## Pinned Section\n"
    "pinned body line 1\n"
    "pinned body line 2\n\n"
    "## Forked Section\n"
    "forked body line 1\n"
    "forked body mid\n"
    "forked body line 3\n\n"
    "## Final checks\n"
    "final intro line\n\n"
    "### Failure handling\n"
    "final failure line 1\n"
    "final failure line 2\n\n"
    "## Deployment\n"
    "deploy intro line\n\n"
    "### Failure handling\n"
    "deploy failure line 1\n"
    "deploy failure line 2\n"
)

# Live body with host edits confined to the forked span region — span-only
# drift with NO stored base (the clobber shape).
_LIVE_FORK_EDITED = _TRACKED_MD_BODY.replace("forked body mid", "FORKED-LIVE-EDIT")


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _install(c: ContainerHandle) -> tuple[int, str, str]:
    return _setforge(
        c,
        ["install", "--profile=test-spans-forked", f"--config={CONFIG_FIXTURE}"],
    )


def _revert(c: ContainerHandle) -> tuple[int, str, str]:
    return _setforge(
        c,
        ["revert", "--profile=test-spans-forked", f"--config={CONFIG_FIXTURE}", "-y"],
    )


def _exists(c: ContainerHandle, path: str) -> bool:
    return c.exec(["test", "-e", path], check=False).returncode == 0


@pytest.mark.xdist_group("docker_daemon")
def test_install_revert_revert_redoes_install(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """install seeds stores → revert undoes (deletes stores, restores live) →
    SECOND revert REDOES the install verbatim.

    The redo leg is the gap: it must re-apply the install-applied state —
    live span edit clobbered again, BOTH store entries re-seeded
    byte-identical to the first install — driven entirely by the reverse
    transition the first revert recorded. A mis-recorded reverse store
    snapshot would leave the live edit (no clobber) or the stores absent.
    """
    c = docker_container()
    # Host edits the forked region BEFORE setforge ever ran here.
    c.write_text(_LIVE_FORKED, _LIVE_FORK_EDITED)

    rc, _stdout, stderr = _install(c)
    assert rc == 0, stderr
    # Base-absent first install deployed tracked VERBATIM over the edit.
    assert c.read_text(_LIVE_FORKED) == _TRACKED_MD_BODY
    base_v1 = c.read_text(_BASE_FORKED)
    sidecar_v1 = c.read_text(_SIDECAR_FORKED)
    assert base_v1, "byte-base must be seeded by the first install"
    assert sidecar_v1, "spans sidecar must be seeded by the first install"

    # First revert: undo the install.
    rc, _stdout, stderr = _revert(c)
    assert rc == 0, stderr
    assert c.read_text(_LIVE_FORKED) == _LIVE_FORK_EDITED
    assert not _exists(c, _BASE_FORKED), "first revert must delete the seeded base"
    assert not _exists(c, _SIDECAR_FORKED), (
        "first revert must delete the seeded sidecar"
    )

    # SECOND revert: REDO the install. The reverse transition recorded by the
    # first revert re-applies the install-applied state.
    rc, _stdout, stderr = _revert(c)
    assert rc == 0, stderr
    # Live is clobbered again, byte-identical to the first install.
    assert c.read_text(_LIVE_FORKED) == _TRACKED_MD_BODY
    # BOTH store entries are re-seeded byte-identical to the first install —
    # proving the reverse store-state recapture round-trips.
    assert c.read_text(_BASE_FORKED) == base_v1
    assert c.read_text(_SIDECAR_FORKED) == sidecar_v1
