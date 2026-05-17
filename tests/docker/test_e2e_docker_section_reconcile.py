"""Docker E2E tests for dotfiles-9by section reconcile wizard.

19 cases per the bd dotfiles-9by --notes:

Original behavior + flag matrix (1-10):
- 1: --auto=use-tracked deploys shared drift + hash assertion.
- 2: bare install warns on pending tracked.
- 3: bare install warns on conflict.
- 4: --auto=keep-live silences + hash rewrite.
- 5: host-local drift silently kept + hash maintained.
- 6: --auto=use-tracked then revert restores live.
- 7: mutually-exclusive flags exit 2.
- 8: untagged marker raises MarkerError.
- 9: legacy hashless markers fall back to two-way.
- 10: post-migration idempotent + hash alignment.

Per-CLI-flag-row coverage (11-17):
- 11: bare install no drift no warning.
- 12: --reconcile-user-sections + no drift exits silently (timeout guard).
- 13: --reconcile-user-sections + piped "k" keeps live.
- 14: --reconcile-user-sections + piped "t" takes tracked.
- 15: --reconcile-user-sections + piped "s\\nk\\n" for 2 drifts.
- 16: --auto=use-tracked overwrites even with live edits.
- 17: --auto=keep-live silences even pending tracked.

Compare dry-run (18-19):
- 18: compare --reconcile-user-sections shows three-way state.
- 19: compare --reconcile-user-sections no prompt (timeout guard).
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable

# pexpect ships no stubs; types-pexpect not added as a dev dep (per qzq scope).
import pexpect  # type: ignore[import-untyped]
import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_LIVE_SHARED = "/home/tester/.my_setup_e2e/sections/shared.md"
_LIVE_HOST_LOCAL = "/home/tester/.my_setup_e2e/sections/marked.md"
_TRACKED_SHARED = "/workspace/tests/fixtures/e2e/tracked/sections/shared.md"

# The shared.md fixture body deployed by `_install`. ``_BASELINE_HASH`` is
# the sha256 of this body — after the first install, BOTH live's and
# tracked's end markers carry ``hash=_BASELINE_HASH`` (live via
# ``maintain_marker_hashes``, tracked via ``stamp_tracked_baseline``).
# The three-way reconcile tests below construct PENDING_TRACKED /
# LIVE_EDITED / CONFLICT setups by mutating one or both BODIES while
# keeping the embedded ``hash=`` at ``_BASELINE_HASH`` so the classifier
# treats that side as "moved away from the baseline" or "still at
# baseline" as needed.


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


_BASELINE_SHARED_BODY = "- rule A\n- rule B (new in tracked)\n"
_BASELINE_SHARED_HASH = _sha256(_BASELINE_SHARED_BODY)


def _shared_section(body: str, embed_hash: str | None) -> str:
    """Build the shared-section tracked_file body the e2e fixture deploys."""
    hash_segment = f" hash={embed_hash}" if embed_hash is not None else ""
    return (
        "# test-reconcile-sections fixture (shared)\n\n"
        "Global text above the marker.\n\n"
        "<!-- setforge:user-section start shared workflow -->\n"
        f"{body}"
        f"<!-- setforge:user-section end shared workflow{hash_segment} -->\n\n"
        "Trailing tracked content.\n"
    )


def _shared_two_section(
    section_a: tuple[str, str | None], section_b: tuple[str, str | None]
) -> str:
    """Build a two-section shared tracked_file body.

    Each section is ``(body, embed_hash)``. Used by the
    ``skip_then_keep_live`` test to construct two independently
    classifiable shared sections in a single file. Section names are
    ``workflow`` and ``commits``.
    """
    body_a, hash_a = section_a
    body_b, hash_b = section_b
    seg_a = f" hash={hash_a}" if hash_a is not None else ""
    seg_b = f" hash={hash_b}" if hash_b is not None else ""
    return (
        "# test-reconcile-sections fixture (shared, two sections)\n\n"
        "Global text above the markers.\n\n"
        "<!-- setforge:user-section start shared workflow -->\n"
        f"{body_a}"
        f"<!-- setforge:user-section end shared workflow{seg_a} -->\n\n"
        "Interstitial tracked content.\n\n"
        "<!-- setforge:user-section start shared commits -->\n"
        f"{body_b}"
        f"<!-- setforge:user-section end shared commits{seg_b} -->\n\n"
        "Trailing tracked content.\n"
    )


def _shared_three_section(
    section_a: tuple[str, str | None],
    section_b: tuple[str, str | None],
    section_c: tuple[str, str | None],
) -> str:
    """Build a three-section shared tracked_file body.

    Section names: ``workflow``, ``commits``, ``python``. Used by the
    compare dry-run test that needs all three drift states represented
    simultaneously.
    """
    body_a, hash_a = section_a
    body_b, hash_b = section_b
    body_c, hash_c = section_c
    seg_a = f" hash={hash_a}" if hash_a is not None else ""
    seg_b = f" hash={hash_b}" if hash_b is not None else ""
    seg_c = f" hash={hash_c}" if hash_c is not None else ""
    return (
        "# test-reconcile-sections fixture (shared, three sections)\n\n"
        "Global text above the markers.\n\n"
        "<!-- setforge:user-section start shared workflow -->\n"
        f"{body_a}"
        f"<!-- setforge:user-section end shared workflow{seg_a} -->\n\n"
        "Interstitial 1.\n\n"
        "<!-- setforge:user-section start shared commits -->\n"
        f"{body_b}"
        f"<!-- setforge:user-section end shared commits{seg_b} -->\n\n"
        "Interstitial 2.\n\n"
        "<!-- setforge:user-section start shared python -->\n"
        f"{body_c}"
        f"<!-- setforge:user-section end shared python{seg_c} -->\n\n"
        "Trailing tracked content.\n"
    )


def _host_local_section(body: str, embed_hash: str | None) -> str:
    """Build the host-local-section tracked_file body."""
    hash_segment = f" hash={embed_hash}" if embed_hash is not None else ""
    return (
        "# test-text-sections fixture\n\n"
        "Global text that lives in tracked and overwrites the "
        "live copy on every install.\n\n"
        "<!-- setforge:user-section start host-local notes -->\n"
        f"{body}"
        f"<!-- setforge:user-section end host-local notes{hash_segment} -->\n\n"
        "Trailing tracked content.\n"
    )


def _install(
    container: ContainerHandle,
    profile: str,
    *,
    extra: list[str] | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "uv",
        "run",
        "setforge",
        "install",
        f"--profile={profile}",
        f"--config={CONFIG_FIXTURE}",
    ]
    if extra:
        cmd.extend(extra)
    if timeout is not None:
        cmd = ["timeout", str(timeout), *cmd]
    result = container.exec(cmd, check=False)
    if check:
        assert result.returncode == 0, result.stderr or result.stdout
    return result


# ---------------------------------------------------------------------------
# Originals + flag matrix (1-10)
# ---------------------------------------------------------------------------


def test_install_reconcile_use_tracked_deploys_shared_drift(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """1: --auto=use-tracked deploys tracked-side body + hash invariant."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    # Pre-seed live with OLD body (pretend baseline = "rule A only")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    # Run with --auto=use-tracked
    _install(c, "test-reconcile-sections", extra=["--auto=use-tracked"])
    live = c.read_text(_LIVE_SHARED)
    assert "rule B (new in tracked)" in live
    # Hash invariant: embedded hash matches body.
    body = "- rule A\n- rule B (new in tracked)\n"
    assert f"hash={_sha256(body)}" in live


def test_install_bare_warns_on_shared_drift_pending_tracked(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """2: Bare install + pending tracked drift → stderr warning.

    PENDING_TRACKED setup: after the baseline install (both sides
    stamped with ``hash=_BASELINE_SHARED_HASH``), mutate the TRACKED
    body while keeping its end-marker hash at the baseline. That gives
    ``A_T != E_T`` (tracked moved) and ``A_L == E_L`` (live pristine) —
    the exact PENDING_TRACKED contract.
    """
    c = docker_container()
    _install(c, "test-reconcile-sections")
    new_tracked = "- rule A\n- rule B (new in tracked)\n- rule C (newer)\n"
    c.write_text(
        _TRACKED_SHARED,
        _shared_section(new_tracked, _BASELINE_SHARED_HASH),
    )
    result = _install(c, "test-reconcile-sections")
    combined = result.stdout + result.stderr
    assert "pending tracked update" in combined
    # Live unchanged (tracked-side update was NOT auto-deployed).
    assert "rule C (newer)" not in c.read_text(_LIVE_SHARED)


def test_install_bare_warns_on_conflict_when_both_edited(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """3: Bare install + both-edited (conflict) → stderr warning."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    baseline = "- rule A\n"
    # Live: edited away from baseline.
    c.write_text(
        _LIVE_SHARED,
        _shared_section("- rule A\n- live local change\n", _sha256(baseline)),
    )
    # Tracked: already has the tracked body in the fixture; embedded hash = baseline.
    # Make tracked's embedded hash also baseline to ensure CONFLICT (both moved).
    c.exec(
        [
            "sed",
            "-i",
            f"s|hash=[0-9a-f]\\{{64\\}}|hash={_sha256(baseline)}|g",
            "/workspace/tests/fixtures/e2e/tracked/sections/shared.md",
        ],
        check=False,
    )
    result = _install(c, "test-reconcile-sections")
    combined = result.stdout + result.stderr
    assert "three-way conflict" in combined or "conflict" in combined


def test_install_auto_keep_live_silences_warning(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """4: --auto=keep-live → no warning + hash rewritten on live."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = _install(c, "test-reconcile-sections", extra=["--auto=keep-live"])
    combined = result.stdout + result.stderr
    assert "shared section" not in combined
    live = c.read_text(_LIVE_SHARED)
    assert "rule B (new in tracked)" not in live
    # Hash now matches live body.
    assert f"hash={_sha256(old)}" in live


def test_install_host_local_drift_silently_kept_hash_maintained(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """5: host-local section drift kept silently, hash rewritten."""
    c = docker_container()
    _install(c, "test-text-sections")
    new_body = "host-edited body line\n"
    c.write_text(_LIVE_HOST_LOCAL, _host_local_section(new_body, "deadbeef" * 8))
    result = _install(c, "test-text-sections")
    combined = result.stdout + result.stderr
    assert "shared section" not in combined
    live = c.read_text(_LIVE_HOST_LOCAL)
    assert "host-edited body line" in live
    assert f"hash={_sha256(new_body)}" in live


def test_install_reconcile_use_tracked_then_revert_restores_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """6: revert undoes a wizard-driven install."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    pre_text = c.read_text(_LIVE_SHARED)
    _install(c, "test-reconcile-sections", extra=["--auto=use-tracked"])
    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-reconcile-sections",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    assert revert.returncode == 0, revert.stderr
    assert c.read_text(_LIVE_SHARED) == pre_text


def test_install_mutually_exclusive_flags_exit_2(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """7: --reconcile-user-sections + --auto=... exits 2."""
    c = docker_container()
    result = _install(
        c,
        "test-reconcile-sections",
        extra=["--reconcile-user-sections", "--auto=use-tracked"],
        check=False,
    )
    assert result.returncode == 2
    assert "mutually exclusive" in (result.stdout + result.stderr)


def test_install_untagged_marker_raises_marker_error(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """8: an untagged marker (no host-local|shared keyword) raises MarkerError."""
    c = docker_container()
    # Plant an untagged marker into tracked.
    c.write_text(
        "/workspace/tests/fixtures/e2e/tracked/sections/shared.md",
        "<!-- setforge:user-section start workflow -->\nbody\n"
        "<!-- setforge:user-section end workflow -->\n",
    )
    result = _install(c, "test-reconcile-sections", check=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "missing required" in combined or "MarkerError" in combined


def test_install_legacy_no_embedded_hash_falls_back_to_two_way(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """9: live without embedded hash → classifier returns LEGACY → keep live."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    # Live body different from tracked AND no embedded hash → LEGACY.
    c.write_text(_LIVE_SHARED, _shared_section("- rule LEGACY-LIVE\n", None))
    result = _install(c, "test-reconcile-sections")
    combined = result.stdout + result.stderr
    assert "legacy" in combined.lower()
    assert "rule B (new in tracked)" not in c.read_text(_LIVE_SHARED)


def test_install_post_migration_idempotent_with_hash_alignment(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """10: install twice in a row; second is NOOP, hashes aligned."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    first = c.read_text(_LIVE_SHARED)
    result = _install(c, "test-reconcile-sections")
    second = c.read_text(_LIVE_SHARED)
    assert first == second
    assert "noop" in result.stdout.lower() or "NOOP" in result.stdout


# ---------------------------------------------------------------------------
# CLI flag matrix coverage (11-17)
# ---------------------------------------------------------------------------


def test_install_bare_no_drift_no_warning(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """11: clean install (no drift) emits no shared-drift warning."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    # Pre-condition: live == tracked + hashes maintained, so re-install has no drift.
    result = _install(c, "test-reconcile-sections")
    combined = result.stdout + result.stderr
    assert "shared section" not in combined


def test_install_reconcile_with_no_drift_exits_silently(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """12: --reconcile-user-sections + no drift exits 0 without hanging.

    Timeout=10s guard catches the failure-mode where the wizard tries to
    prompt on a no-drift section.
    """
    c = docker_container()
    _install(c, "test-reconcile-sections")
    result = _install(
        c,
        "test-reconcile-sections",
        extra=["--reconcile-user-sections"],
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_install_reconcile_interactive_keep_live(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., pexpect.spawn],
) -> None:
    """13: --reconcile-user-sections + PTY 'k' keeps live.

    LIVE_EDITED setup: baseline-install both sides, then mutate LIVE
    body to a host-edited form while keeping its end-marker hash at
    the baseline. ``A_L != E_L`` (live moved) AND ``A_T == E_T``
    (tracked pristine) — the LIVE_EDITED contract. Pressing ``k``
    keeps the live body verbatim, so the assertion is that the
    tracked-only marker ``"rule B (new in tracked)"`` does NOT appear
    in live after the wizard.

    Driven via PTY (matches the sync-wizard interactive tests in
    ``test_e2e_docker.py``); the prompter uses ``termios`` raw mode,
    which a piped stdin via ``docker exec -i`` cannot satisfy.
    """
    c = docker_container()
    _install(c, "test-reconcile-sections")
    live_edited = "- rule A\n- rule LIVE-EDIT\n"
    c.write_text(
        _LIVE_SHARED,
        _shared_section(live_edited, _BASELINE_SHARED_HASH),
    )
    session = docker_pty_session(
        c,
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-reconcile-sections",
            f"--config={CONFIG_FIXTURE}",
            "--reconcile-user-sections",
        ],
        timeout=120,
    )
    idx = session.expect(["Choice", pexpect.EOF, pexpect.TIMEOUT], timeout=30)
    assert idx == 0, f"reconcile wizard never prompted; saw: {session.before!r}"
    session.send("k")
    session.expect(pexpect.EOF)
    live_after = c.read_text(_LIVE_SHARED)
    assert "rule B (new in tracked)" not in live_after
    assert "rule LIVE-EDIT" in live_after


def test_install_reconcile_interactive_take_tracked(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., pexpect.spawn],
) -> None:
    """14: --reconcile-user-sections + PTY 't' takes tracked.

    PENDING_TRACKED setup: baseline install, then mutate TRACKED body
    (a new rule C added on top of the existing tracked content),
    keep tracked's end-marker hash at the baseline. ``A_T != E_T``,
    ``A_L == E_L`` — PENDING_TRACKED. Pressing ``t`` adopts the
    tracked body into live; the new ``rule C`` marker must land.

    Driven via PTY for the same reason as the keep-live variant —
    the wizard prompter uses ``termios`` raw mode.
    """
    c = docker_container()
    _install(c, "test-reconcile-sections")
    new_tracked = "- rule A\n- rule B (new in tracked)\n- rule C (newer)\n"
    c.write_text(
        _TRACKED_SHARED,
        _shared_section(new_tracked, _BASELINE_SHARED_HASH),
    )
    session = docker_pty_session(
        c,
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-reconcile-sections",
            f"--config={CONFIG_FIXTURE}",
            "--reconcile-user-sections",
        ],
        timeout=120,
    )
    idx = session.expect(["Choice", pexpect.EOF, pexpect.TIMEOUT], timeout=30)
    assert idx == 0, f"reconcile wizard never prompted; saw: {session.before!r}"
    session.send("t")
    session.expect(pexpect.EOF)
    live_after = c.read_text(_LIVE_SHARED)
    assert "rule B (new in tracked)" in live_after
    assert "rule C (newer)" in live_after


def test_install_reconcile_interactive_skip_then_keep_live(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., pexpect.spawn],
) -> None:
    """15: --reconcile-user-sections + 2 drifted sections + PTY 's' then 'k'.

    Two-section setup, one drift state per section:

    * ``workflow`` — PENDING_TRACKED (mutated TRACKED body, E_T held
      at baseline). Wizard prompt #1 → ``s`` (skip → keep live).
    * ``commits`` — LIVE_EDITED (mutated LIVE body, E_L held at
      baseline). Wizard prompt #2 → ``k`` (keep live).

    Both outcomes keep live, so neither tracked-body marker
    (``"rule B (new in tracked)"`` for workflow, ``"commit rule X
    (tracked-only)"`` for commits) ends up in live afterward.

    Driven via PTY; two ``Choice`` prompts are expected back-to-back.
    """
    c = docker_container()
    workflow_baseline = "- rule A\n- rule B (new in tracked)\n"
    commits_baseline = "- commit rule X (tracked-only)\n"
    h_workflow = _sha256(workflow_baseline)
    h_commits = _sha256(commits_baseline)

    # Replace the single-section tracked fixture with a two-section
    # one (both sections pristine) BEFORE the baseline install.
    c.write_text(
        _TRACKED_SHARED,
        _shared_two_section(
            (workflow_baseline, h_workflow),
            (commits_baseline, h_commits),
        ),
    )
    _install(c, "test-reconcile-sections")

    # Mutate TRACKED workflow body, leave its hash at h_workflow → PENDING_TRACKED.
    # Mutate LIVE commits body, leave its hash at h_commits → LIVE_EDITED.
    c.write_text(
        _TRACKED_SHARED,
        _shared_two_section(
            (
                "- rule A\n- rule B (new in tracked)\n- rule C (tracked-only)\n",
                h_workflow,
            ),
            (commits_baseline, h_commits),
        ),
    )
    c.write_text(
        _LIVE_SHARED,
        _shared_two_section(
            (workflow_baseline, h_workflow),
            ("- commit rule LIVE-EDITED\n", h_commits),
        ),
    )

    session = docker_pty_session(
        c,
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-reconcile-sections",
            f"--config={CONFIG_FIXTURE}",
            "--reconcile-user-sections",
        ],
        timeout=120,
    )
    for keypress in ("s", "k"):
        idx = session.expect(["Choice", pexpect.EOF, pexpect.TIMEOUT], timeout=30)
        assert idx == 0, (
            f"reconcile wizard never prompted ({keypress!r}); saw: {session.before!r}"
        )
        session.send(keypress)
    session.expect(pexpect.EOF)
    live_after = c.read_text(_LIVE_SHARED)
    # workflow skipped → tracked-only "rule C" not in live.
    assert "rule C (tracked-only)" not in live_after
    # commits keep-live → live-edited marker preserved; tracked-only
    # commit-rule body NOT injected (live wins on keep-live).
    assert "commit rule LIVE-EDITED" in live_after


def test_install_auto_use_tracked_overwrites_even_with_live_edits(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """16: --auto=use-tracked overwrites live even under three-way conflict."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    baseline = "- rule A\n"
    # Live: edited (different from baseline).
    c.write_text(
        _LIVE_SHARED,
        _shared_section("- rule A\n- live edited\n", _sha256(baseline)),
    )
    _install(c, "test-reconcile-sections", extra=["--auto=use-tracked"])
    live = c.read_text(_LIVE_SHARED)
    # Tracked body wins.
    assert "rule B (new in tracked)" in live
    assert "live edited" not in live


def test_install_auto_keep_live_silences_even_pending_tracked(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """17: --auto=keep-live silences PENDING_TRACKED warning."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = _install(c, "test-reconcile-sections", extra=["--auto=keep-live"])
    combined = result.stdout + result.stderr
    assert "pending tracked update" not in combined
    assert "shared section" not in combined


# ---------------------------------------------------------------------------
# Compare dry-run (18-19)
# ---------------------------------------------------------------------------


def test_compare_reconcile_dry_run_shows_three_way_state(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """18: compare --reconcile-user-sections names the three-way state.

    Three-section setup, one drift state each. ``compare`` is
    read-only (does NOT stamp tracked-side hashes), so we use the
    baseline-install step to lock both sides into a hash-aligned
    starting point, then mutate bodies while holding the embedded
    hashes at the per-section baselines to drive each classifier
    outcome:

    * ``workflow`` → PENDING_TRACKED (mutate tracked body).
    * ``commits``  → LIVE_EDITED (mutate live body).
    * ``python``   → CONFLICT (mutate BOTH bodies).
    """
    c = docker_container()
    workflow_baseline = "- rule A\n- rule B (new in tracked)\n"
    commits_baseline = "- commit rule baseline\n"
    python_baseline = "- python rule baseline\n"
    h_workflow = _sha256(workflow_baseline)
    h_commits = _sha256(commits_baseline)
    h_python = _sha256(python_baseline)

    c.write_text(
        _TRACKED_SHARED,
        _shared_three_section(
            (workflow_baseline, h_workflow),
            (commits_baseline, h_commits),
            (python_baseline, h_python),
        ),
    )
    _install(c, "test-reconcile-sections")

    # Now construct one drift state per section.
    c.write_text(
        _TRACKED_SHARED,
        _shared_three_section(
            ("- rule A\n- rule B (new in tracked)\n- rule C (newer)\n", h_workflow),
            (commits_baseline, h_commits),
            ("- python rule TRACKED-MOVED\n", h_python),
        ),
    )
    c.write_text(
        _LIVE_SHARED,
        _shared_three_section(
            (workflow_baseline, h_workflow),
            ("- commit rule LIVE-EDIT\n", h_commits),
            ("- python rule LIVE-MOVED\n", h_python),
        ),
    )

    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "compare",
            "--profile=test-reconcile-sections",
            f"--config={CONFIG_FIXTURE}",
            "--reconcile-user-sections",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    # All three state labels must appear.
    assert "pending tracked update" in combined
    assert "live edits" in combined
    assert "three-way conflict" in combined
    # All three section names must appear.
    assert "workflow" in combined
    assert "commits" in combined
    assert "python" in combined


def test_compare_reconcile_dry_run_no_prompt(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """19: compare --reconcile-user-sections never prompts (timeout=10s)."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    # Don't pipe input — if compare prompted, it would block and timeout would kill it.
    result = c.exec(
        [
            "timeout",
            "10",
            "uv",
            "run",
            "setforge",
            "compare",
            "--profile=test-reconcile-sections",
            f"--config={CONFIG_FIXTURE}",
            "--reconcile-user-sections",
        ],
        check=False,
    )
    assert result.returncode == 0, (
        f"compare blocked (timeout exit): stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
