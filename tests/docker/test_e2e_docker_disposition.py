"""Docker e2e tests for the file-level ``disposition`` reconciliation model.

Exercises the opt-in per-file ``disposition: shared|forked|pinned`` field
end-to-end against a fresh Debian container with the actual installed
``setforge`` CLI, through the non-interactive install / sync / compare
surfaces. The interactive hunk-resolution wizard is out of scope and is
NOT tested here.

Behavior under exercise:

- **shared** — install 3-way merges {stored-base, live, tracked}; a clean
  (non-overlapping) merge lands both edits; a same-line conflict under bare
  install keeps live + warns + succeeds; ``--auto=use-tracked`` takes the
  tracked value. ``sync`` captures live edits back to tracked.
- **forked** — install 3-way merges (gets upstream tracked updates) but
  ``sync`` NEVER captures live edits back.
- **pinned** — install never overwrites the live file (host-owned).
- **compare** — reports each file's disposition and classifies forked /
  pinned drift as expected.

The stored base seeds from tracked bytes on the first install (no base
yet); it lives under ``state_root()/base/<profile>/<file_id>``.

Profiles under exercise (declared in
``tests/fixtures/e2e/setforge.test.yaml``):
``test-disposition-shared`` / ``test-disposition-forked`` /
``test-disposition-pinned`` / ``test-disposition-forked-yaml``.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Tracked source (inside the container workspace) shared by the three
# markdown disposition fixtures — editing it simulates an upstream change.
_TRACKED_MD = "/workspace/tests/fixtures/e2e/tracked/disposition/note.md"
_TRACKED_YAML = "/workspace/tests/fixtures/e2e/tracked/disposition/config.yaml"

# Live destinations (one per disposition so their stored bases never cross).
_LIVE_SHARED = "/home/tester/.setforge_e2e/disposition/shared.md"
_LIVE_FORKED = "/home/tester/.setforge_e2e/disposition/forked.md"
_LIVE_PINNED = "/home/tester/.setforge_e2e/disposition/pinned.md"
_LIVE_FORKED_YAML = "/home/tester/.setforge_e2e/disposition/forked.yaml"

# The canonical markdown tracked body (matches the fixture src on disk).
_TRACKED_MD_BODY = "# Disposition fixture\n\nintro line\nmiddle line\nfooter line\n"

# A live YAML body that diverges from the forked-yaml fixture src.
_FORKED_YAML_LIVE = (
    "# Disposition YAML fixture.\ntrackedKey: tracked-value\nsharedKey: live-edit\n"
)


def _setforge(
    c: ContainerHandle, args: list[str], *, check: bool = False
) -> tuple[int, str, str]:
    """Run ``uv run setforge <args>`` and return (returncode, stdout, stderr)."""
    result = c.exec(["uv", "run", "setforge", *args], check=check)
    return result.returncode, result.stdout, result.stderr


def _install(
    c: ContainerHandle, profile: str, *, extra: list[str] | None = None
) -> tuple[int, str, str]:
    """Run ``setforge install --profile=<profile> --config=<fixture>``."""
    args = ["install", f"--profile={profile}", f"--config={CONFIG_FIXTURE}"]
    if extra:
        args.extend(extra)
    return _setforge(c, args)


def _sync(c: ContainerHandle, profile: str) -> tuple[int, str, str]:
    """Run ``setforge sync --profile=<profile> --config=<fixture> -y``."""
    return _setforge(
        c, ["sync", f"--profile={profile}", f"--config={CONFIG_FIXTURE}", "-y"]
    )


# ---------------------------------------------------------------------------
# shared — clean non-conflicting 3-way merge
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_shared_clean_three_way_merge(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """shared: first install seeds base; disjoint live + tracked edits merge.

    Install once (live == tracked, base seeded). Then edit the LIVE file's
    footer region and the TRACKED file's intro region — non-overlapping
    hunks. The second install 3-way merges and the live file carries BOTH
    edits.
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-disposition-shared")
    assert rc == 0, stderr
    assert c.read_text(_LIVE_SHARED) == _TRACKED_MD_BODY
    # Base seeded == tracked bytes (first install, no prior base).
    base = c.exec(
        [
            "cat",
            "/home/tester/.local/state/setforge/base/"
            "test-disposition-shared/disposition_shared_md",
        ]
    ).stdout
    assert base == _TRACKED_MD_BODY, base

    # Live edits the FOOTER; tracked edits the INTRO — disjoint hunks.
    c.write_text(
        _LIVE_SHARED,
        "# Disposition fixture\n\nintro line\nmiddle line\nfooter-LIVE\n",
    )
    c.write_text(
        _TRACKED_MD,
        "# Disposition fixture\n\nintro-TRACKED\nmiddle line\nfooter line\n",
    )
    rc, _stdout, stderr = _install(c, "test-disposition-shared")
    assert rc == 0, stderr
    merged = c.read_text(_LIVE_SHARED)
    assert "intro-TRACKED" in merged, merged
    assert "footer-LIVE" in merged, merged


_SHARED_BASE_PATH = (
    "/home/tester/.local/state/setforge/base/"
    "test-disposition-shared/disposition_shared_md"
)


@pytest.mark.xdist_group("docker_daemon")
def test_shared_auto_migrate_on_install_warns_and_reverts(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A marker-bearing live auto-migrates on first install: warn + revertible.

    Pre-seed the live file with legacy SHARED user-section markers (the
    pre-disposition world). The first disposition install strips them, seeds a
    per-host base from the stripped live, and emits a one-time warning. A single
    ``setforge revert`` restores the marker-bearing live AND removes the seeded
    base in lockstep — the auto-on-install migration round-trip.
    """
    c = docker_container()
    marker_live = (
        "# Disposition fixture\n\n"
        "<!-- setforge:user-section start shared S -->\n"
        "intro line\nmiddle line\nfooter line\n"
        "<!-- setforge:user-section end shared S -->\n"
    )
    c.exec(["mkdir", "-p", "/home/tester/.setforge_e2e/disposition"])
    c.write_text(_LIVE_SHARED, marker_live)

    rc, stdout, stderr = _install(c, "test-disposition-shared")
    assert rc == 0, stderr
    combined = stdout + stderr
    # The one-time auto-migration warning fired, and markers were stripped.
    assert "first install under a stored-base disposition" in combined, combined
    assert "user-section" not in c.read_text(_LIVE_SHARED), c.read_text(_LIVE_SHARED)
    assert c.exec(["test", "-f", _SHARED_BASE_PATH], check=False).returncode == 0

    # Revert restores the marker-bearing live AND removes the seeded base.
    rc, _o, stderr = _setforge(
        c,
        [
            "revert",
            "--profile=test-disposition-shared",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ],
    )
    assert rc == 0, stderr
    assert "user-section" in c.read_text(_LIVE_SHARED), c.read_text(_LIVE_SHARED)
    assert c.exec(["test", "-f", _SHARED_BASE_PATH], check=False).returncode != 0


# ---------------------------------------------------------------------------
# shared — same-line conflict, bare install defers (keeps live, warns, succeeds)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_shared_conflict_bare_install_keeps_live_and_warns(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """shared: same-line live + tracked edit under bare install keeps live.

    Bare install (no --auto) defers the conflict: the live edit is kept, a
    conflict warning is emitted, and the command still exits 0 (defer, not
    abort).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-disposition-shared")
    assert rc == 0, stderr

    # Both sides edit the SAME middle line → conflict.
    c.write_text(
        _LIVE_SHARED,
        "# Disposition fixture\n\nintro line\nmiddle-LIVE\nfooter line\n",
    )
    c.write_text(
        _TRACKED_MD,
        "# Disposition fixture\n\nintro line\nmiddle-TRACKED\nfooter line\n",
    )
    rc, stdout, stderr = _install(c, "test-disposition-shared")
    # Bare install DEFERS the conflict — it does not abort.
    assert rc == 0, stderr
    # Live kept its own edit.
    assert "middle-LIVE" in c.read_text(_LIVE_SHARED)
    # Conflict warning emitted (the warning lands on stderr).
    assert "conflict" in (stdout + stderr).lower(), stdout + stderr


# ---------------------------------------------------------------------------
# shared — same-line conflict, --auto=use-tracked takes tracked
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_shared_conflict_auto_use_tracked_takes_tracked(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """shared: the same conflict under --auto=use-tracked takes the tracked value."""
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-disposition-shared")
    assert rc == 0, stderr

    c.write_text(
        _LIVE_SHARED,
        "# Disposition fixture\n\nintro line\nmiddle-LIVE\nfooter line\n",
    )
    c.write_text(
        _TRACKED_MD,
        "# Disposition fixture\n\nintro line\nmiddle-TRACKED\nfooter line\n",
    )
    rc, _stdout, stderr = _install(
        c, "test-disposition-shared", extra=["--auto=use-tracked"]
    )
    assert rc == 0, stderr
    live = c.read_text(_LIVE_SHARED)
    assert "middle-TRACKED" in live, live
    assert "middle-LIVE" not in live, live


# ---------------------------------------------------------------------------
# forked — sync never captures live edits back to tracked
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_forked_sync_never_captures_back(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """forked: install merges, but sync NEVER writes live edits to tracked.

    Install once, then edit the live file and run sync. The tracked source
    MUST remain byte-identical to its original — forked is upstream-followed
    on install but never captured back.
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-disposition-forked")
    assert rc == 0, stderr
    assert c.read_text(_TRACKED_MD) == _TRACKED_MD_BODY

    # Edit live, then sync.
    c.write_text(
        _LIVE_FORKED,
        "# Disposition fixture\n\nintro line\nmiddle-LIVE\nfooter line\n",
    )
    rc, _stdout, stderr = _sync(c, "test-disposition-forked")
    assert rc == 0, stderr
    # Tracked source unchanged — forked never captures back.
    assert c.read_text(_TRACKED_MD) == _TRACKED_MD_BODY, c.read_text(_TRACKED_MD)


# ---------------------------------------------------------------------------
# shared — sync captures live edits back to tracked; re-install is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_shared_sync_captures_back_and_reinstall_clean(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """shared: sync captures the live edit into tracked; next install is clean.

    Install once, edit live, run sync — the tracked source now reflects the
    live edit. A subsequent install converges with no conflict warning
    (base == tracked == live).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-disposition-shared")
    assert rc == 0, stderr

    captured = "# Disposition fixture\n\nintro line\nmiddle-CAPTURED\nfooter line\n"
    c.write_text(_LIVE_SHARED, captured)
    rc, _stdout, stderr = _sync(c, "test-disposition-shared")
    assert rc == 0, stderr
    # Tracked source now carries the live edit (captured).
    assert "middle-CAPTURED" in c.read_text(_TRACKED_MD), c.read_text(_TRACKED_MD)

    # A subsequent install is a clean no-op: no conflict warning.
    rc, stdout, stderr = _install(c, "test-disposition-shared")
    assert rc == 0, stderr
    assert "conflict" not in (stdout + stderr).lower(), stdout + stderr
    assert c.read_text(_LIVE_SHARED) == captured, c.read_text(_LIVE_SHARED)


# ---------------------------------------------------------------------------
# pinned — install never overwrites the live file
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_pinned_install_never_overwrites_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """pinned: a tracked change does NOT overwrite the host-owned live file.

    Seed a live file with host-only content, change the tracked source,
    then install. The live file keeps its host content untouched.
    """
    c = docker_container()
    # Pre-seed a live file the install must NOT clobber.
    c.write_text(_LIVE_PINNED, "PINNED-LIVE-WINS\n")
    # Change tracked away from the live content.
    c.write_text(_TRACKED_MD, "# Disposition fixture\n\nupstream-CHANGED\n")
    rc, _stdout, stderr = _install(c, "test-disposition-pinned")
    assert rc == 0, stderr
    # Live untouched — pinned is host-owned.
    assert c.read_text(_LIVE_PINNED) == "PINNED-LIVE-WINS\n", c.read_text(_LIVE_PINNED)


@pytest.mark.xdist_group("docker_daemon")
def test_pinned_fresh_host_first_install_deploys_tracked(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """pinned fresh host (no base, no live): first install deploys the tracked
    bytes (NOT an empty file); the second install never rewrites the file.

    The no-write probe is sub-second mtime equality (``stat %.Y``): deploy's
    NOOP detection skips the write entirely when content matches, so any
    rewrite — even of identical bytes — would advance the fractional mtime.
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-disposition-pinned")
    assert rc == 0, stderr
    assert c.read_text(_LIVE_PINNED) == _TRACKED_MD_BODY, c.read_text(_LIVE_PINNED)
    mtime_first = c.exec(["stat", "-c", "%.Y", _LIVE_PINNED], check=True).stdout.strip()
    rc2, _stdout2, stderr2 = _install(c, "test-disposition-pinned")
    assert rc2 == 0, stderr2
    assert c.read_text(_LIVE_PINNED) == _TRACKED_MD_BODY
    mtime_second = c.exec(
        ["stat", "-c", "%.Y", _LIVE_PINNED], check=True
    ).stdout.strip()
    assert mtime_second == mtime_first, "second install must not rewrite the file"


# ---------------------------------------------------------------------------
# compare — reports disposition and marks forked drift as expected
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_compare_reports_forked_drift_as_expected(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """compare: a forked file's drift carries the disposition and is expected.

    Install the forked YAML file, then diverge live from tracked. Compare's
    JSON payload reports ``disposition: "forked"`` and ``drift_is_expected:
    true`` for the file, and ``compare --check`` exits 0 (expected drift is
    not flagged).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-disposition-forked-yaml")
    assert rc == 0, stderr

    # Diverge live from tracked.
    c.write_text(_LIVE_FORKED_YAML, _FORKED_YAML_LIVE)
    rc, stdout, stderr = _setforge(
        c,
        [
            "-o",
            "json",
            "compare",
            "--profile=test-disposition-forked-yaml",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    payload = json.loads(stdout)["data"]
    entries = {e["name"]: e for e in payload["entries"]}
    assert "disposition_forked_yaml" in entries, payload
    entry = entries["disposition_forked_yaml"]
    assert entry["disposition"] == "forked", entry
    assert entry["status"] == "drifted", entry
    assert entry["drift_is_expected"] is True, entry
    # Forked drift is expected → no unexpected drift overall.
    assert payload["has_unexpected_drift"] is False, payload

    # compare --check exits 0 because the only drift is expected.
    rc, _stdout, stderr = _setforge(
        c,
        [
            "compare",
            "--profile=test-disposition-forked-yaml",
            f"--config={CONFIG_FIXTURE}",
            "--check",
        ],
    )
    assert rc == 0, stderr


@pytest.mark.xdist_group("docker_daemon")
def test_compare_classifies_stale_after_tracked_advance(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """compare: tracked advancing after an install classifies the file stale.

    Install the shared file (base seeded == tracked == live), then change
    only the TRACKED source — live still equals the stored base. Compare's
    JSON payload reports ``drift_class: "stale"`` with a reason, the drift
    is not unexpected, and ``compare --check`` exits 0 (a stale deploy is
    install's job to fix, not a CI failure).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-disposition-shared")
    assert rc == 0, stderr
    assert c.read_text(_LIVE_SHARED) == _TRACKED_MD_BODY

    # Advance TRACKED only; live keeps the last-deployed bytes (== base).
    c.write_text(
        _TRACKED_MD,
        "# Disposition fixture\n\nintro-TRACKED-v2\nmiddle line\nfooter line\n",
    )
    rc, stdout, stderr = _setforge(
        c,
        [
            "-o",
            "json",
            "compare",
            "--profile=test-disposition-shared",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, stderr
    payload = json.loads(stdout)["data"]
    entries = {e["name"]: e for e in payload["entries"]}
    assert "disposition_shared_md" in entries, payload
    entry = entries["disposition_shared_md"]
    assert entry["status"] == "drifted", entry
    assert entry["drift_class"] == "stale", entry
    assert "install will update" in entry["reason"], entry
    # Stale drift is not unexpected drift.
    assert payload["has_unexpected_drift"] is False, payload

    # compare --check exits 0 because stale-only drift passes the gate.
    rc, _stdout, stderr = _setforge(
        c,
        [
            "compare",
            "--profile=test-disposition-shared",
            f"--config={CONFIG_FIXTURE}",
            "--check",
        ],
    )
    assert rc == 0, stderr
