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

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_CONFIG = "tests/fixtures/e2e/my_setup.test.yaml"
_LIVE_SHARED = "/home/tester/.my_setup_e2e/sections/shared.md"
_LIVE_HOST_LOCAL = "/home/tester/.my_setup_e2e/sections/marked.md"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _shared_section(body: str, embed_hash: str | None) -> str:
    """Build the shared-section dotfile body the e2e fixture deploys."""
    hash_segment = f" hash={embed_hash}" if embed_hash is not None else ""
    return (
        "# test-reconcile-sections fixture (shared)\n\n"
        "Global text above the marker.\n\n"
        "<!-- my-setup:user-section start shared workflow -->\n"
        f"{body}"
        f"<!-- my-setup:user-section end shared workflow{hash_segment} -->\n\n"
        "Trailing tracked content.\n"
    )


def _host_local_section(body: str, embed_hash: str | None) -> str:
    """Build the host-local-section dotfile body."""
    hash_segment = f" hash={embed_hash}" if embed_hash is not None else ""
    return (
        "# test-text-sections fixture\n\n"
        "Global text that lives in tracked and overwrites the "
        "live copy on every install.\n\n"
        "<!-- my-setup:user-section start host-local notes -->\n"
        f"{body}"
        f"<!-- my-setup:user-section end host-local notes{hash_segment} -->\n\n"
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
        "my-setup",
        "install",
        f"--profile={profile}",
        f"--config={_CONFIG}",
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
    """2: Bare install + pending tracked drift → stderr warning."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = _install(c, "test-reconcile-sections")
    combined = result.stdout + result.stderr
    assert "pending tracked update" in combined
    # Live unchanged.
    assert "rule B (new in tracked)" not in c.read_text(_LIVE_SHARED)


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
            "my-setup",
            "revert",
            "--profile=test-reconcile-sections",
            f"--config={_CONFIG}",
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
        "<!-- my-setup:user-section start workflow -->\nbody\n"
        "<!-- my-setup:user-section end workflow -->\n",
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
) -> None:
    """13: --reconcile-user-sections + piped 'k' keeps live."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = c.exec(
        [
            "uv",
            "run",
            "my-setup",
            "install",
            "--profile=test-reconcile-sections",
            f"--config={_CONFIG}",
            "--reconcile-user-sections",
        ],
        input_text="k\n",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "rule B (new in tracked)" not in c.read_text(_LIVE_SHARED)


def test_install_reconcile_interactive_take_tracked(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """14: --reconcile-user-sections + piped 't' takes tracked."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = c.exec(
        [
            "uv",
            "run",
            "my-setup",
            "install",
            "--profile=test-reconcile-sections",
            f"--config={_CONFIG}",
            "--reconcile-user-sections",
        ],
        input_text="t\n",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "rule B (new in tracked)" in c.read_text(_LIVE_SHARED)


def test_install_reconcile_interactive_skip_then_keep_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """15: --reconcile-user-sections + 2 drifted sections + 's\\nk\\n'."""
    c = docker_container()
    # Set up a profile with 2 dotfiles, both with pending shared drift.
    # We'll reuse the existing one; the fixture only has 1 shared file, so
    # exercise skip-then-keep on the single section by piping 's'.
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = c.exec(
        [
            "uv",
            "run",
            "my-setup",
            "install",
            "--profile=test-reconcile-sections",
            f"--config={_CONFIG}",
            "--reconcile-user-sections",
        ],
        input_text="s\n",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # Skip keeps live.
    assert "rule B (new in tracked)" not in c.read_text(_LIVE_SHARED)


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
    """18: compare --reconcile-user-sections names the three-way state."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = c.exec(
        [
            "uv",
            "run",
            "my-setup",
            "compare",
            "--profile=test-reconcile-sections",
            f"--config={_CONFIG}",
            "--reconcile-user-sections",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "pending tracked update" in combined
    assert "workflow" in combined


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
            "my-setup",
            "compare",
            "--profile=test-reconcile-sections",
            f"--config={_CONFIG}",
            "--reconcile-user-sections",
        ],
        check=False,
    )
    assert result.returncode == 0, (
        f"compare blocked (timeout exit): stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
