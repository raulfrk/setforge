"""Docker e2e tests for the sub-file pinned / forked span engine (p5qc.8).

Exercises the span mechanism end-to-end against a fresh Debian container
with the actual installed ``setforge`` CLI, through the non-interactive
install / sync / compare / revert surfaces. The span engine rides the
disposition stored-base 3-way merge path: a span freezes (``pinned``) or
host-isolates (``forked``) a markdown heading-scoped region with NO in-file
marker.

Behavior under exercise (the spec's acceptance + the three leak vectors):

- **install** deploys a pinned span body; the live span body is preserved
  across an upstream edit ELSEWHERE in the file over TWO installs with no
  phantom conflict and a byte-stable live region (the cross-install
  round-trip, Invariants I1 / I3).
- **capture / sync** excludes a pinned span body from a tracked writeback
  (leak vector 1: ``_capture_disposition_file`` SHARED writeback), AND
  ``sync --auto=use-live`` does not absorb the span body (leak vector 2),
  AND a forked span is excluded from capture but still merges upstream
  (leak vector 3) — Invariant I2 (capture exclusion total).
- **compare** marks the span region as expected-drift (Invariant I13).
- **orphan** — an upstream heading rename / delete orphans a pinned span:
  bare install WARNS and still exits 0 (Invariant I6); ``--strict-spans``
  makes it exit non-zero.
- **revert** rolls live + byte-base + spans sidecar in lockstep
  (Invariant I5).

Profiles under exercise (declared in
``tests/fixtures/e2e/setforge.test.yaml``): ``test-spans-pinned`` (a
pinned span on ``## Pinned Section``) and ``test-spans-forked`` (a forked
span on ``## Forked Section``). Both share src ``spans/note.md``; editing
that src (per fresh container) simulates an upstream change elsewhere.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

# Tracked source (inside the container workspace) shared by both span
# fixtures — editing it simulates an upstream change ELSEWHERE in the file.
_TRACKED_MD = "/workspace/tests/fixtures/e2e/tracked/spans/note.md"

# Live destinations (one per span scenario so their stored base + spans
# sidecar never cross).
_LIVE_PINNED = "/home/tester/.setforge_e2e/spans/pinned.md"
_LIVE_FORKED = "/home/tester/.setforge_e2e/spans/forked.md"

# Per-host derived state (byte base + spans sidecar) for the pinned file.
_BASE_PINNED = (
    "/home/tester/.local/state/setforge/base/test-spans-pinned/spans_pinned_md"
)
_SIDECAR_PINNED = (
    "/home/tester/.local/state/setforge/spans/test-spans-pinned/spans_pinned_md.json"
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

# The pinned region body (heading inclusive) the user freezes locally.
_PINNED_REGION = "## Pinned Section\npinned body line 1\npinned body line 2\n\n"
_PINNED_REGION_LIVE = "## Pinned Section\nPINNED-LIVE-EDIT-1\nPINNED-LIVE-EDIT-2\n\n"


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


def _sync(
    c: ContainerHandle, profile: str, *, extra: list[str] | None = None
) -> tuple[int, str, str]:
    """Run ``setforge sync --profile=<profile> --config=<fixture> -y``."""
    args = ["sync", f"--profile={profile}", f"--config={CONFIG_FIXTURE}", "-y"]
    if extra:
        args.extend(extra)
    return _setforge(c, args)


# ---------------------------------------------------------------------------
# install — pinned span survives an upstream edit elsewhere across TWO
# installs with no phantom conflict and a byte-stable live region.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_pinned_span_cross_install_roundtrip_byte_stable(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """pinned span: live region preserved across two installs, no phantom.

    Install once (live == tracked, base + sidecar seeded). Edit the LIVE
    pinned region (host freeze) AND, on a SECOND install with an upstream
    edit ELSEWHERE in the file (the ``## Upstream`` section), the merge
    lands the upstream edit while the pinned region keeps the LIVE bytes.
    A THIRD install with no new edits is a clean no-op — no phantom
    conflict (Invariant I1: base re-baselined to post-splice bytes) and
    the live pinned region is byte-stable (Invariant I3).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr
    assert c.read_text(_LIVE_PINNED) == _TRACKED_MD_BODY

    # User freezes the pinned region with host-only edits.
    live_frozen = _TRACKED_MD_BODY.replace(_PINNED_REGION, _PINNED_REGION_LIVE)
    c.write_text(_LIVE_PINNED, live_frozen)

    # Upstream edits the UNRELATED ## Upstream section.
    upstream_edited = _TRACKED_MD_BODY.replace(
        "upstream line A\n", "upstream line A — UPSTREAM CHANGED\n"
    )
    c.write_text(_TRACKED_MD, upstream_edited)

    rc, stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr
    merged = c.read_text(_LIVE_PINNED)
    # Upstream edit landed (merge applied outside the span).
    assert "UPSTREAM CHANGED" in merged, merged
    # Pinned region kept LIVE bytes (post-merge override, live wins).
    assert "PINNED-LIVE-EDIT-1" in merged, merged
    assert "pinned body line 1" not in merged, merged

    # THIRD install, no new edits → clean no-op, NO phantom conflict.
    rc, stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr
    assert "conflict" not in (stdout + stderr).lower(), stdout + stderr
    # Live pinned region byte-stable across the round-trip.
    assert c.read_text(_LIVE_PINNED) == merged, c.read_text(_LIVE_PINNED)


# ---------------------------------------------------------------------------
# capture (leak vector 1) — sync excludes a pinned span body from tracked.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_pinned_span_excluded_from_sync_capture(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """leak vector 1: a pinned span body NEVER bakes into the tracked src.

    Install, edit BOTH the pinned region AND an unrelated region in live,
    then sync. The tracked src must capture the unrelated edit but the
    PINNED region must stay byte-identical to its original tracked body —
    a host-local span body never leaks into the shared config repo
    (Invariant I2).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr

    # Edit the pinned region AND the unrelated ## Upstream region in live.
    live = _TRACKED_MD_BODY.replace(_PINNED_REGION, _PINNED_REGION_LIVE).replace(
        "upstream line B\n", "upstream line B — LIVE EDIT\n"
    )
    c.write_text(_LIVE_PINNED, live)
    rc, _stdout, stderr = _sync(c, "test-spans-pinned")
    assert rc == 0, stderr

    tracked = c.read_text(_TRACKED_MD)
    # The unrelated edit captured back to tracked.
    assert "upstream line B — LIVE EDIT" in tracked, tracked
    # The pinned region did NOT leak into tracked.
    assert "PINNED-LIVE-EDIT-1" not in tracked, tracked
    assert "pinned body line 1" in tracked, tracked


# ---------------------------------------------------------------------------
# capture (leak vector 2) — sync --auto=use-live does not absorb the span.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_pinned_span_not_absorbed_by_sync_auto_use_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """leak vector 2: ``sync --auto=use-live`` never absorbs a span body.

    Even the silent-absorb drift path must skip the pinned region: edit
    only the pinned region in live, then ``sync --auto=use-live``. The
    tracked src keeps the original pinned body (Invariant I2 — capture
    exclusion total across BOTH the writeback AND the drift-absorption
    path).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr

    live = _TRACKED_MD_BODY.replace(_PINNED_REGION, _PINNED_REGION_LIVE)
    c.write_text(_LIVE_PINNED, live)
    rc, _stdout, stderr = _sync(c, "test-spans-pinned", extra=["--auto=use-live"])
    assert rc == 0, stderr

    tracked = c.read_text(_TRACKED_MD)
    assert "PINNED-LIVE-EDIT-1" not in tracked, tracked
    assert "pinned body line 1" in tracked, tracked


# ---------------------------------------------------------------------------
# capture (leak vector 3) — a forked span is excluded from capture but
# still merges upstream.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_forked_span_excluded_from_capture_but_merges_upstream(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """leak vector 3: a forked span merges upstream yet never captures back.

    A forked span gets NO merge override (it follows upstream) but IS
    excluded from capture. Install, edit the forked region in live + edit
    it upstream too (disjoint lines), then verify: a second install merges
    the upstream forked-region edit into live, while sync never writes the
    live forked edit back to tracked (Invariant I2).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-spans-forked")
    assert rc == 0, stderr
    assert c.read_text(_LIVE_FORKED) == _TRACKED_MD_BODY

    # Upstream edits the forked region (line 1); live edits a DIFFERENT,
    # non-adjacent line of it (line 3) with the untouched ``forked body
    # mid`` line between them, so the 3-way merge is clean (no conflict).
    upstream = _TRACKED_MD_BODY.replace(
        "forked body line 1\n", "forked body line 1 — UPSTREAM\n"
    )
    c.write_text(_TRACKED_MD, upstream)
    live = _TRACKED_MD_BODY.replace(
        "forked body line 3\n", "forked body line 3 — LIVE\n"
    )
    c.write_text(_LIVE_FORKED, live)

    # Second install: forked span merges upstream (no override).
    rc, _stdout, stderr = _install(c, "test-spans-forked")
    assert rc == 0, stderr
    merged = c.read_text(_LIVE_FORKED)
    assert "forked body line 1 — UPSTREAM" in merged, merged
    assert "forked body line 3 — LIVE" in merged, merged

    # Sync must NOT capture the live forked edit back to tracked.
    rc, _stdout, stderr = _sync(c, "test-spans-forked")
    assert rc == 0, stderr
    tracked = c.read_text(_TRACKED_MD)
    assert "forked body line 3 — LIVE" not in tracked, tracked


# ---------------------------------------------------------------------------
# compare — the span region reports as expected-drift.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_compare_marks_pinned_span_drift_expected(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """compare: drift confined to a pinned span is expected, not flagged.

    Install, then diverge ONLY the pinned region in live. ``compare
    --check`` exits 0 because the only drift lives inside the span
    (Invariant I13).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr

    live = _TRACKED_MD_BODY.replace(_PINNED_REGION, _PINNED_REGION_LIVE)
    c.write_text(_LIVE_PINNED, live)

    rc, _stdout, stderr = _setforge(
        c,
        [
            "compare",
            "--profile=test-spans-pinned",
            f"--config={CONFIG_FIXTURE}",
            "--check",
        ],
    )
    # Span-confined drift is expected → compare --check exits 0.
    assert rc == 0, stderr


# ---------------------------------------------------------------------------
# orphan — upstream heading rename orphans a pinned span: bare install
# warns + exits 0; --strict-spans exits non-zero.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_orphaned_pinned_span_warns_and_install_succeeds(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """orphan: an upstream heading rename warns but install still exits 0.

    Install once (span resolved). Then RENAME the pinned heading upstream
    so the anchor goes missing — live is left at the tracked body so the
    3-way merge cleanly applies the rename (no conflict), leaving the
    merged text WITHOUT the ``## Pinned Section`` anchor. The span thus
    orphans: bare install WARNS (region preserved, not dropped) and still
    exits 0 (Invariant I6).
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr

    # Upstream RENAMES the pinned heading → anchor gone; live unchanged so
    # the merge cleanly takes the rename (no same-region conflict).
    c.write_text(
        _TRACKED_MD,
        _TRACKED_MD_BODY.replace("## Pinned Section", "## Renamed Section"),
    )
    rc, stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr
    combined = (stdout + stderr).lower()
    assert "span" in combined, stdout + stderr
    assert "could not be relocated" in combined, stdout + stderr


@pytest.mark.xdist_group("docker_daemon")
def test_orphaned_pinned_span_strict_exits_nonzero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """orphan + --strict-spans: a pinned orphan escalates to refuse-install."""
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr

    # Same clean-rename orphan setup as the warn case; --strict-spans turns
    # the pinned orphan into a non-zero refuse-install.
    c.write_text(
        _TRACKED_MD,
        _TRACKED_MD_BODY.replace("## Pinned Section", "## Renamed Section"),
    )
    rc, stdout, stderr = _install(c, "test-spans-pinned", extra=["--strict-spans"])
    assert rc != 0, stdout + stderr
    assert "strict-spans" in (stdout + stderr).lower(), stdout + stderr


# ---------------------------------------------------------------------------
# revert — live + byte-base + spans sidecar roll in lockstep.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_revert_rolls_live_base_and_sidecar_in_lockstep(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """revert: live, byte-base, and spans sidecar restore atomically (I5).

    Install once (live / base / sidecar at state v1). Then freeze the live
    pinned region and edit upstream, and install AGAIN (state v2 — the
    base + sidecar advance to the post-splice bytes). ``revert`` of the
    most recent (v2) install must restore ALL THREE — live, the stored
    byte-base, and the spans sidecar manifest — back to their pre-v2-install
    state in lockstep, none lagging.

    The byte-base + sidecar pre-v2 equal their v1 values (only an install
    writes them, and the host's live edit between installs does not). Live
    pre-v2 is the FROZEN live the user wrote — revert restores that exact
    pre-install snapshot, not the pristine first-install body.
    """
    c = docker_container()
    rc, _stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr
    base_v1 = c.read_text(_BASE_PINNED)
    sidecar_v1 = c.read_text(_SIDECAR_PINNED)
    assert base_v1, "byte-base must exist after first install"
    assert sidecar_v1, "spans sidecar must exist after first install"

    # Drive state forward: freeze live pinned region + edit upstream. The
    # frozen live is the exact pre-v2-install live snapshot revert restores.
    live_pre_v2 = _TRACKED_MD_BODY.replace(_PINNED_REGION, _PINNED_REGION_LIVE)
    c.write_text(_LIVE_PINNED, live_pre_v2)
    c.write_text(
        _TRACKED_MD,
        _TRACKED_MD_BODY.replace(
            "upstream line A\n", "upstream line A — V2 UPSTREAM\n"
        ),
    )
    rc, _stdout, stderr = _install(c, "test-spans-pinned")
    assert rc == 0, stderr
    # State advanced on all three artifacts at v2.
    assert c.read_text(_LIVE_PINNED) != live_pre_v2
    assert c.read_text(_BASE_PINNED) != base_v1
    assert c.read_text(_SIDECAR_PINNED) != sidecar_v1

    # Revert the most recent install → all three roll back in lockstep.
    rc, _stdout, stderr = _setforge(
        c,
        ["revert", "--profile=test-spans-pinned", f"--config={CONFIG_FIXTURE}", "-y"],
    )
    assert rc == 0, stderr
    assert c.read_text(_LIVE_PINNED) == live_pre_v2, c.read_text(_LIVE_PINNED)
    assert c.read_text(_BASE_PINNED) == base_v1, c.read_text(_BASE_PINNED)
    assert c.read_text(_SIDECAR_PINNED) == sidecar_v1, c.read_text(_SIDECAR_PINNED)
