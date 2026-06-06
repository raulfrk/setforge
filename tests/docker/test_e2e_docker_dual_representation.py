"""Docker e2e: legacy preserve_user_sections and disposition coexist in one config.

Proves the expand-window guarantee end-to-end — a single profile carrying BOTH
reconciliation models (a legacy ``preserve_user_sections`` markdown file AND a
``disposition: shared`` markdown file) installs in one run with each file's
behavior intact and no cross-file interference: the run-global keep-set prune
retains the disposition base, and the distinct ``dst`` paths keep the per-host
bases from ever crossing.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-dual-representation"
_LIVE_SECTIONS = "/home/tester/.setforge_e2e/sections/marked.md"
_LIVE_DISPOSITION = "/home/tester/.setforge_e2e/disposition/shared.md"
_DISPOSITION_BASE = (
    "/home/tester/.local/state/setforge/base/"
    "test-dual-representation/disposition_shared_md"
)
_TRACKED_MD_BODY = "# Disposition fixture\n\nintro line\nmiddle line\nfooter line\n"


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
    """Both reconciliation models install in one run; bases stay independent.

    First install: the disposition file seeds its stored base from tracked and
    the preserve file lands with its markers. Then edit the live host-local
    section (always preserved) AND the live disposition footer (disjoint hunk →
    clean 3-way) and re-install: the host edit survives (preserve model intact)
    and the footer edit survives (disposition model intact) — proving the two
    models ran independently in the same install. The disposition base existing
    after the first run proves the run-global keep-set prune did not drop it.
    """
    c = docker_container()

    rc, _out, err = _install(c)
    assert rc == 0, err
    assert c.read_text(_LIVE_DISPOSITION) == _TRACKED_MD_BODY
    # Independence guard: the run-global keep-set prune retained the disposition
    # base even though the profile also carries a non-disposition (preserve) file.
    assert c.read_text(_DISPOSITION_BASE) == _TRACKED_MD_BODY
    # The preserve file deployed with its user-section markers intact.
    sections_live = c.read_text(_LIVE_SECTIONS)
    assert "setforge:user-section start" in sections_live, sections_live

    # Edit the live host-local section (always preserved) and the live
    # disposition footer (disjoint hunk), then re-install.
    edited = sections_live.replace("default notes (tracked side)", "MY HOST EDIT")
    assert edited != sections_live, sections_live
    c.write_text(_LIVE_SECTIONS, edited)
    c.write_text(
        _LIVE_DISPOSITION,
        "# Disposition fixture\n\nintro line\nmiddle line\nfooter-LIVE\n",
    )
    rc2, _out2, err2 = _install(c)
    assert rc2 == 0, err2

    # Preserve model intact: the host edit survived.
    assert "MY HOST EDIT" in c.read_text(_LIVE_SECTIONS)
    # Disposition model intact: the live footer survived the 3-way merge.
    assert "footer-LIVE" in c.read_text(_LIVE_DISPOSITION)

    # compare sees BOTH models in one profile without error.
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
    assert {"sections_md", "disposition_shared_md"} <= entries, entries
