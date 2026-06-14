"""Docker e2e tests for the ``setforge snapshot {create,list,restore}`` CLI.

Closes the e2e-coverage gap flagged by the audit: the snapshot subgroup
is a real, filesystem-MUTATING command set (per CLAUDE.md the
``tests/docker/`` ring is the canonical behavior-preservation gate) yet
had only inner-ring CliRunner coverage. None of the live-mutation
semantics — additive-overlay restore, ``--keep`` retention, the non-TTY
mutate-gate, or a create→mutate→restore roundtrip — was exercised
end-to-end against a real container until now.

The 'snapshot' tokens elsewhere in ``tests/docker/`` refer to internal
install/revert STATE snapshots (see
``test_e2e_docker_state_snapshots.py``), not this user-facing CLI.

Profile: ``test-minimal`` (single byte-copy tracked file
``minimal_text`` → ``~/.setforge_e2e/minimal/text.txt``) — the floor
case, so the assertions isolate snapshot behavior from overlay/merge
machinery.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-minimal"
_LIVE = "/home/tester/.setforge_e2e/minimal/text.txt"
_LIVE_ONLY = "/home/tester/.setforge_e2e/minimal/live-only.txt"
_SNAP_ROOT = "/home/tester/.local/share/setforge/snapshots"
# Canonical tracked body (matches tests/fixtures/e2e/tracked/minimal/text.txt).
_TRACKED_BODY = "hello from test-minimal\n"


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _install(c: ContainerHandle) -> tuple[int, str, str]:
    return _setforge(
        c, ["install", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"]
    )


def _snapshot(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    return _setforge(c, ["snapshot", *args], check=check)


def _exists(c: ContainerHandle, path: str) -> bool:
    return c.exec(["test", "-e", path], check=False).returncode == 0


def _snapshot_dirs(c: ContainerHandle) -> list[str]:
    """Return the finalized snapshot dir names under the snapshot root.

    Filters ``.partial`` scratch dirs; returns ``[]`` when the root does
    not yet exist (``ls`` exits nonzero on a missing dir).
    """
    res = c.exec(["ls", "-1", _SNAP_ROOT], check=False)
    if res.returncode != 0:
        return []
    return [
        line
        for line in res.stdout.splitlines()
        if line.strip() and not line.endswith(".partial")
    ]


@pytest.mark.xdist_group("docker_daemon")
def test_snapshot_create_then_list_shows_row(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``snapshot create`` writes a dir under the root; ``list`` prints its row."""
    c = docker_container()
    rc, _out, err = _install(c)
    assert rc == 0, err

    rc, out, err = _snapshot(
        c,
        [
            "create",
            "before-experiment",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, err

    dirs = _snapshot_dirs(c)
    assert len(dirs) == 1, dirs
    # id shape is ``<YYYYMMDDTHHMMSSZ>-<label>``.
    assert dirs[0].endswith("-before-experiment"), dirs[0]
    # The captured tracked file mirrors under the snapshot tree.
    mirror = f"{_SNAP_ROOT}/{dirs[0]}{_LIVE}"
    assert _exists(c, mirror), mirror
    assert c.read_text(mirror) == _TRACKED_BODY

    rc, out, err = _snapshot(c, ["list"])
    assert rc == 0, err
    assert "before-experiment" in out, out


@pytest.mark.xdist_group("docker_daemon")
def test_snapshot_restore_yes_overlays_live_additively(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``restore --yes`` overlays snapshot files; live-only files untouched."""
    c = docker_container()
    rc, _out, err = _install(c)
    assert rc == 0, err

    rc, _out, err = _snapshot(
        c,
        ["create", "pristine", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"],
    )
    assert rc == 0, err

    # Mutate the tracked live file AND add a brand-new live-only file that
    # the snapshot never captured.
    c.write_text(_LIVE, "MUTATED LIVE CONTENT\n")
    c.write_text(_LIVE_ONLY, "live-only survivor\n")

    rc, _out, err = _snapshot(
        c,
        [
            "restore",
            "pristine",
            "--yes",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, err

    # Tracked file is back to the captured bytes; live-only file untouched.
    assert c.read_text(_LIVE) == _TRACKED_BODY
    assert _exists(c, _LIVE_ONLY), "additive restore must leave live-only files alone"
    assert c.read_text(_LIVE_ONLY) == "live-only survivor\n"


@pytest.mark.xdist_group("docker_daemon")
def test_snapshot_restore_non_tty_no_yes_refuses(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-TTY ``restore`` without ``--yes`` exits nonzero and mutates nothing."""
    c = docker_container()
    rc, _out, err = _install(c)
    assert rc == 0, err

    rc, _out, err = _snapshot(
        c,
        ["create", "guarded", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"],
    )
    assert rc == 0, err

    # Drift the live file so we can prove no restore happened.
    c.write_text(_LIVE, "DRIFTED\n")

    # ``c.exec`` runs without a PTY → stdin is not a TTY. Bare restore must
    # refuse rather than silently proceed.
    rc, _out, err = _snapshot(
        c,
        [
            "restore",
            "guarded",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc != 0, "non-TTY restore without --yes must exit nonzero"
    assert "requires --yes" in (err + _out), (err, _out)
    # Zero live mutation: the drifted content is still in place.
    assert c.read_text(_LIVE) == "DRIFTED\n"


@pytest.mark.xdist_group("docker_daemon")
def test_snapshot_create_keep_prunes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``create --keep=1`` retains only the newest snapshot after a second create."""
    c = docker_container()
    rc, _out, err = _install(c)
    assert rc == 0, err

    rc, _out, err = _snapshot(
        c,
        [
            "create",
            "older",
            "--keep=1",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, err
    # Sleep ≥1s so the second snapshot's second-resolution id timestamp is
    # strictly greater (ids are ``<YYYYMMDDTHHMMSSZ>-<label>``), making the
    # newest-first prune deterministic and avoiding an id collision.
    c.exec(["sleep", "1.2"], check=True)
    rc, _out, err = _snapshot(
        c,
        [
            "create",
            "newer",
            "--keep=1",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, err

    dirs = _snapshot_dirs(c)
    assert len(dirs) == 1, dirs
    assert dirs[0].endswith("-newer"), dirs[0]
