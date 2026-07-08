"""Docker E2E tests for the --auto* confirmation wizard.

Coverage matrix:

install --auto-accept-tracked (legacy unexpected drift, tracked direction):
  - with-yes / non-tty-no-yes

install --auto-accept-live (legacy unexpected drift, live direction):
  - with-yes / non-tty-no-yes

sync --auto=use-live (capture):
  - with-yes / non-tty-no-yes-exit-1

Negative coverage (confirm must NOT fire):
  - bare install / sync (no --auto*)
  - install --auto=keep-live
  - sync --auto=keep-tracked

Cross-cutting:
  - install --auto=use-tracked + --yes revert roundtrip
  - empty-drift no-op (no confirm fires)
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker

_LIVE_SHARED = "/home/tester/.setforge_e2e/sections/shared.md"
_TRACKED_SHARED = "/workspace/tests/fixtures/e2e/tracked/sections/shared.md"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


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


def _install(
    container: ContainerHandle,
    profile: str,
    *,
    extra: list[str] | None = None,
    check: bool = True,
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
    result = container.exec(cmd, check=False)
    if check:
        assert result.returncode == 0, result.stderr or result.stdout
    return result


def _sync(
    container: ContainerHandle,
    profile: str,
    *,
    extra: list[str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "uv",
        "run",
        "setforge",
        "sync",
        f"--profile={profile}",
        f"--config={CONFIG_FIXTURE}",
    ]
    if extra:
        cmd.extend(extra)
    result = container.exec(cmd, check=False)
    if check:
        assert result.returncode == 0, result.stderr or result.stdout
    return result


# ---------------------------------------------------------------------------
# install --auto-accept-tracked (unexpected drift, tracked direction)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_auto_accept_tracked_with_yes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--auto-accept-tracked --yes: applies, exit 0."""
    c = docker_container()
    # jsonc-shallow (a plain JSONC reconcile file). Replacing the live file
    # with an unrelated key is a live-only edit; the retired confirm gate no
    # longer fires, so the apply is a clean no-op that preserves the edit.
    _install(c, "test-jsonc-shallow")
    live_path = c.exec(
        ["bash", "-c", "ls /home/tester/.setforge_e2e/jsonc/*.json | head -1"],
    ).stdout.strip()
    assert live_path, (
        "jsonc fixture missing — investigate (was test-jsonc-shallow "
        "profile in fixtures/e2e/setforge.test.yaml removed or renamed?)"
    )
    c.exec(["bash", "-c", f"echo '{{\"unexpected_new_key\": 1}}' > {live_path}"])
    result = _install(
        c,
        "test-jsonc-shallow",
        extra=["--auto-accept-tracked", "--yes"],
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    # No-op: preserved local edit → no transition, no revert hint.
    assert "noop" in result.stdout
    assert "↩  revert with" not in result.stdout


def test_install_auto_accept_tracked_non_tty_no_yes_exit_1(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-TTY + --auto-accept-tracked on absorbed drift → clean exit 0.

    The legacy unexpected-drift confirm gate (keyed on the now-removed
    preserve_* ``unexpected_drift_keys``) no longer fires, so
    --auto-accept-tracked is a clean no-op apply needing no --yes. The
    pre-2.0 exit-1 require---yes behavior is gone with the preserve_*
    contraction.
    """
    c = docker_container()
    _install(c, "test-jsonc-shallow")
    live_path = c.exec(
        ["bash", "-c", "ls /home/tester/.setforge_e2e/jsonc/*.json | head -1"],
    ).stdout.strip()
    assert live_path, (
        "jsonc fixture missing — investigate (was test-jsonc-shallow "
        "profile in fixtures/e2e/setforge.test.yaml removed or renamed?)"
    )
    c.exec(["bash", "-c", f"echo '{{\"unexpected_new_key\": 1}}' > {live_path}"])
    result = _install(
        c,
        "test-jsonc-shallow",
        extra=["--auto-accept-tracked"],
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


# ---------------------------------------------------------------------------
# install --auto-accept-live (unexpected drift, live direction)
# ---------------------------------------------------------------------------


def test_install_auto_accept_live_with_yes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--auto-accept-live --yes: applies, exit 0."""
    c = docker_container()
    _install(c, "test-jsonc-shallow")
    live_path = c.exec(
        ["bash", "-c", "ls /home/tester/.setforge_e2e/jsonc/*.json | head -1"],
    ).stdout.strip()
    assert live_path, (
        "jsonc fixture missing — investigate (was test-jsonc-shallow "
        "profile in fixtures/e2e/setforge.test.yaml removed or renamed?)"
    )
    c.exec(["bash", "-c", f"echo '{{\"unexpected_new_key\": 1}}' > {live_path}"])
    result = _install(
        c,
        "test-jsonc-shallow",
        extra=["--auto-accept-live", "--yes"],
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    # No-op: preserved local edit → no transition, no revert hint.
    assert "noop" in result.stdout
    assert "↩  revert with" not in result.stdout


def test_install_auto_accept_live_non_tty_no_yes_exit_1(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-TTY + --auto-accept-live on absorbed drift → clean exit 0.

    Mirror of the --auto-accept-tracked case: the legacy unexpected-drift
    confirm gate is gone with the preserve_* contraction, so the live
    direction is likewise a clean no-op apply needing no --yes.
    """
    c = docker_container()
    _install(c, "test-jsonc-shallow")
    live_path = c.exec(
        ["bash", "-c", "ls /home/tester/.setforge_e2e/jsonc/*.json | head -1"],
    ).stdout.strip()
    assert live_path, (
        "jsonc fixture missing — investigate (was test-jsonc-shallow "
        "profile in fixtures/e2e/setforge.test.yaml removed or renamed?)"
    )
    c.exec(["bash", "-c", f"echo '{{\"unexpected_new_key\": 1}}' > {live_path}"])
    result = _install(
        c,
        "test-jsonc-shallow",
        extra=["--auto-accept-live"],
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


# ---------------------------------------------------------------------------
# sync --auto=use-live (capture)
# ---------------------------------------------------------------------------


def test_sync_auto_use_live_with_yes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """sync --auto=use-live --yes: captures, prints revert hint.

    Uses a plain markdown reconcile file whose live edit IS captured back
    into tracked, exercising the sync capture-back transition writer.
    """
    c = docker_container()
    _install(c, "test-disposition-shared")
    live_path = "/home/tester/.setforge_e2e/disposition/shared.md"
    c.write_text(
        live_path,
        "# Disposition fixture\n\nintro line\nmiddle line\nLIVE EDIT footer\n",
    )
    result = _sync(
        c,
        "test-disposition-shared",
        extra=["--auto=use-live", "--yes"],
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    # sync writes the transition hint on success; absence means the
    # gate aborted upstream.
    assert "↩  revert with" in result.stdout


def test_sync_auto_use_live_non_tty_no_yes_exit_1(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-TTY + sync --auto=use-live without --yes → exit 1."""
    c = docker_container()
    _install(c, "test-jsonc-shallow")
    live_path = c.exec(
        ["bash", "-c", "ls /home/tester/.setforge_e2e/jsonc/*.json | head -1"],
    ).stdout.strip()
    assert live_path, (
        "jsonc fixture missing — investigate (was test-jsonc-shallow "
        "profile in fixtures/e2e/setforge.test.yaml removed or renamed?)"
    )
    c.exec(["bash", "-c", f"echo '{{\"new_live_key\": 42}}' > {live_path}"])
    result = _sync(
        c,
        "test-jsonc-shallow",
        extra=["--auto=use-live"],
        check=False,
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "--yes" in combined


# ---------------------------------------------------------------------------
# Negative coverage: confirm must NOT fire
# ---------------------------------------------------------------------------


def test_install_bare_no_auto_no_confirm(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Bare install: no confirm prompt, no exit 1 in non-TTY."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    # Re-install (no drift): exit 0, no confirm.
    result = _install(c, "test-reconcile-sections")
    combined = result.stdout + result.stderr
    assert "confirmation required" not in combined
    assert "Proceed with the mutation" not in combined


@pytest.mark.xdist_group("docker_daemon")
def test_install_auto_keep_live_no_confirm(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--auto=keep-live: non-mutating, no confirm."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = _install(c, "test-reconcile-sections", extra=["--auto=keep-live"])
    combined = result.stdout + result.stderr
    assert "confirmation required" not in combined


def test_sync_bare_no_auto_no_confirm(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Bare sync: no confirm prompt."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    # Bare sync with no drift will fire the merge wizard interactively;
    # in non-TTY it raises CaptureRequiresInteractive (exit 1) — that's
    # pre-existing behavior, not confirm-gate behavior. We only assert
    # the confirm panel is NOT in the output.
    result = _sync(c, "test-reconcile-sections", check=False)
    combined = result.stdout + result.stderr
    assert "confirmation required" not in combined


def test_sync_auto_keep_tracked_no_confirm(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """sync --auto=keep-tracked: non-mutating, no confirm."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    result = _sync(c, "test-reconcile-sections", extra=["--auto=keep-tracked"])
    combined = result.stdout + result.stderr
    assert "confirmation required" not in combined


# ---------------------------------------------------------------------------
# Cross-cutting scenarios
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_auto_use_tracked_live_only_edit_is_noop(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A live-only section edit + --auto=use-tracked is a clean no-op.

    base==tracked (static fixture), so only live diverged: the 3-way reconcile
    preserves the local edit — no conflict, no change, no transition emitted.
    """
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    pre = c.read_text(_LIVE_SHARED)
    result = _install(
        c, "test-reconcile-sections", extra=["--auto=use-tracked", "--yes"]
    )
    assert "↩  revert with" not in result.stdout
    assert c.read_text(_LIVE_SHARED) == pre


def test_install_empty_drift_with_auto_no_confirm_no_op(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """No drift + --auto=use-tracked → empty plan short-circuits, no prompt."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    # Re-install with --auto=use-tracked when there's no drift — empty plan
    # short-circuits in the confirm helper, so no panel appears and exit 0.
    result = _install(
        c, "test-reconcile-sections", extra=["--auto=use-tracked"], check=False
    )
    assert result.returncode == 0, result.stderr or result.stdout
    combined = result.stdout + result.stderr
    assert "Proceed with the mutation" not in combined
