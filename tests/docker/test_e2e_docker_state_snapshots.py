"""Docker e2e test for the transition state-snapshot recovery promise.

Exercises the install → revert → re-install round-trip against a fresh
Debian container with the actual installed ``setforge`` CLI: a first
install seeds the per-host stores (byte base + spans sidecar); ``revert``
must restore the live file AND delete the seeded store entries, so the
re-install repeats the first run verbatim. A stranded base — the bug the
snapshot mechanism closes — would route the re-install through the 3-way
merge instead, silently preserving the live span edit and diverging from
the first-run behavior.

The scenario uses the FORKED span profile (``test-spans-forked``): a
forked span merges upstream with no post-merge override, so the
base-absent first install deploys tracked verbatim over the live span
edit — making "repeats the first run" observably different from "3-way
merges against a stale ancestor" (which would keep the edit).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Live destination + per-host derived state for the forked-span profile.
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
    "forked body line 3\n"
)

# The live body with host edits confined to the forked span region —
# span-only drift with NO stored base: the clobber shape.
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


def _exists(c: ContainerHandle, path: str) -> bool:
    return c.exec(["test", "-e", path], check=False).returncode == 0


@pytest.mark.xdist_group("docker_daemon")
def test_install_revert_reinstall_repeats_first_run_verbatim(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """install seeds the stores → revert deletes them + restores live →
    re-install repeats the first run verbatim.

    The live file pre-exists with a forked-span edit and no stored base.
    The first install clobbers the edit (base-absent verbatim deploy) and
    seeds base + sidecar. After revert, the live span edit is back and
    BOTH store entries are gone — so the re-install clobbers again,
    byte-identical to the first run, instead of 3-way-preserving the
    edit against a stranded ancestor.
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

    rc, _stdout, stderr = _setforge(
        c,
        ["revert", "--profile=test-spans-forked", f"--config={CONFIG_FIXTURE}", "-y"],
    )
    assert rc == 0, stderr
    # Live is back to the span-edited pre-install content...
    assert c.read_text(_LIVE_FORKED) == _LIVE_FORK_EDITED
    # ...and the seeded store entries are DELETED, not stranded.
    assert not _exists(c, _BASE_FORKED), "revert must delete the seeded base"
    assert not _exists(c, _SIDECAR_FORKED), "revert must delete the seeded sidecar"

    # Re-install behaves exactly as the first run: the forked edit is
    # clobbered AGAIN and the stores re-seed byte-identically. A stranded
    # base would instead keep FORKED-LIVE-EDIT via the 3-way merge.
    rc, _stdout, stderr = _install(c)
    assert rc == 0, stderr
    assert c.read_text(_LIVE_FORKED) == _TRACKED_MD_BODY
    assert c.read_text(_BASE_FORKED) == base_v1
    assert c.read_text(_SIDECAR_FORKED) == sidecar_v1
