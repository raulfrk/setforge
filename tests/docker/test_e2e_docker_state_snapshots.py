"""Docker e2e test for the transition state-snapshot recovery promise.

Exercises the install → revert → re-install round-trip against a fresh
Debian container with the actual installed ``setforge`` CLI: a first
install of a plain reconcile file deploys the tracked bytes and seeds the
per-host merge base; ``revert`` must restore the pre-install live state
(here: file absent) AND delete the seeded base, so the re-install repeats
the first run verbatim. A stranded base — the bug the snapshot mechanism
closes — would route the re-install through the 3-way merge against a
stale ancestor instead of repeating the base-absent first deploy.

The scenario uses the ``test-spans-forked`` profile, now a plain markdown
reconcile file under the unified engine (the retired disposition/spans
model left the profile in place; it is an ordinary tracked_file today).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Live destination + per-host merge base for the reconcile profile.
_LIVE_FORKED = "/home/tester/.setforge_e2e/spans/forked.md"
_BASE_FORKED = (
    "/home/tester/.local/state/setforge/base/test-spans-forked/spans_forked_md"
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
    """install deploys + seeds the base → revert deletes it + restores the
    pre-install live state → re-install repeats the first run verbatim.

    The live file does not exist before setforge runs. The first install
    deploys the tracked body and seeds the per-host merge base. After
    revert, the live file is gone again and the seeded base is DELETED,
    not stranded — so the re-install deploys byte-identically to the first
    run instead of 3-way-merging against a stranded ancestor.
    """
    c = docker_container()

    rc, _stdout, stderr = _install(c)
    assert rc == 0, stderr
    # First install deployed the tracked body and seeded the merge base.
    assert c.read_text(_LIVE_FORKED) == _TRACKED_MD_BODY
    base_v1 = c.read_text(_BASE_FORKED)
    assert base_v1, "byte-base must be seeded by the first install"

    rc, _stdout, stderr = _setforge(
        c,
        ["revert", "--profile=test-spans-forked", f"--config={CONFIG_FIXTURE}", "-y"],
    )
    assert rc == 0, stderr
    # Live is back to the pre-install state (absent)...
    assert not _exists(c, _LIVE_FORKED), "revert must restore the pre-install absence"
    # ...and the seeded base is DELETED, not stranded.
    assert not _exists(c, _BASE_FORKED), "revert must delete the seeded base"

    # Re-install behaves exactly as the first run: the tracked body is
    # deployed AGAIN and the base re-seeds byte-identically. A stranded
    # base would instead route through a 3-way merge.
    rc, _stdout, stderr = _install(c)
    assert rc == 0, stderr
    assert c.read_text(_LIVE_FORKED) == _TRACKED_MD_BODY
    assert c.read_text(_BASE_FORKED) == base_v1

    # Exercise a real writer failure after its first bootstrap write. The
    # second bootstrap's parent is deliberately a regular file, so install
    # must use its write-ahead journal to remove the first created path while
    # preserving the pre-existing blocker and clearing the completed recovery.
    recovery_config = "/tmp/setforge-recovery.yaml"
    first_parent = "/home/tester/.setforge_recovery_created"
    first_bootstrap = f"{first_parent}/first"
    blocker = "/home/tester/.setforge_recovery/blocker"
    c.write_text(
        recovery_config,
        "schema_version: '6.0'\n"
        "version: 1\n"
        "tracked_files: {}\n"
        "profiles:\n"
        "  recovery-e2e:\n"
        "    bootstrap:\n"
        f"      - {first_bootstrap}\n"
        f"      - {blocker}/child\n",
    )
    c.write_text(blocker, "keep\n")

    rc, _stdout, stderr = _setforge(
        c,
        [
            "install",
            "--profile=recovery-e2e",
            f"--config={recovery_config}",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
            "--yes",
        ],
    )

    assert rc != 0, "the deliberately invalid second bootstrap must fail"
    assert "automatic recovery failed" not in stderr
    assert not _exists(c, first_bootstrap)
    assert not _exists(c, first_parent)
    assert c.read_text(blocker) == "keep\n"
    rc, _stdout, stderr = _setforge(c, ["recover", "--profile=recovery-e2e"])
    assert rc != 0
    assert "no unfinished operation" in stderr
