"""Docker e2e: a host-local OVERLAY file and a ``disposition: shared`` file
coexist in one install.

Proves end-to-end that a single profile carrying BOTH 2.0 reconciliation
models — a markerless host-local OVERLAY (``host_local_md``, driven by
local.yaml ``host_local_sections``) and a ``disposition: shared`` file
(``disposition_shared_md``, 3-way merged against a per-host stored base) —
installs in one run with each model's behavior intact and no cross-file
interference: the overlay body is (re-)injected markerless on every install
while the disposition file's live footer edit survives through its own
independent stored base. The distinct ``dst`` paths keep the per-host base
from ever crossing, and the run-global keep-set prune retains the disposition
base despite the overlay file in the same run.
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
_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"

# The host-local overlay the dual-representation profile injects markerless
# into ``host_local_md`` (anchored below the fixture's ``## Workflow`` heading).
_OVERLAY = (
    "tracked_files:\n"
    "  host_local_md:\n"
    "    host_local_sections:\n"
    "      coexist:\n"
    "        anchor: {kind: after-heading, value: Workflow}\n"
    "        body: |\n"
    "          OVERLAY COEXIST BODY\n"
)


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
    """Overlay + disposition coexist in one install; neither model interferes.

    First install: the host-local overlay injects its body MARKERLESS into
    ``host_local_md`` while ``disposition_shared_md`` seeds its own per-host
    stored base from tracked. Then edit the disposition live footer (disjoint
    from the overlay file) and re-install: the disposition file's 3-way merge
    keeps the live footer AND the overlay body re-injects — proving the two
    models ran independently in the same install. The disposition base
    carrying the disposition fixture (not the host-local file) proves the
    per-host bases never crossed.
    """
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, _OVERLAY)

    rc, _out, err = _install(c)
    assert rc == 0, err
    # The disposition file seeded its own per-host base; the run-global keep-set
    # prune retained it despite the overlay file processed in the same run, and
    # the base carries the DISPOSITION fixture body (bases never crossed).
    disposition_base = c.read_text(_DISPOSITION_BASE)
    assert "Disposition fixture" in disposition_base, disposition_base
    # The host-local overlay body injected MARKERLESS (no user-section marker).
    host_live = c.read_text(_LIVE_HOST_LOCAL)
    assert "OVERLAY COEXIST BODY" in host_live, host_live
    assert "setforge:user-section" not in host_live, host_live

    # Edit the disposition live footer (disjoint from the overlay file), then
    # re-install.
    c.write_text(
        _LIVE_DISPOSITION,
        "# Disposition fixture\n\nintro line\nmiddle line\nfooter-LIVE\n",
    )
    rc2, _out2, err2 = _install(c)
    assert rc2 == 0, err2

    # Disposition 3-way merge kept the live footer; the overlay body re-injected
    # markerless — the two models did not interfere across the run.
    assert "footer-LIVE" in c.read_text(_LIVE_DISPOSITION)
    host_live2 = c.read_text(_LIVE_HOST_LOCAL)
    assert "OVERLAY COEXIST BODY" in host_live2, host_live2
    assert "setforge:user-section" not in host_live2, host_live2

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
