"""Docker E2E tests for the --auto* confirmation wizard.

Coverage matrix:

install --auto=use-tracked (section reconcile):
  - with-yes / pty-yes / pty-no / non-tty-no-yes-exit-1

install --auto-accept-tracked (legacy unexpected drift, tracked direction):
  - with-yes / non-tty-no-yes-exit-1

install --auto-accept-live (legacy unexpected drift, live direction):
  - with-yes / non-tty-no-yes-exit-1

sync --auto=use-live (capture):
  - with-yes / non-tty-no-yes-exit-1

Negative coverage (confirm must NOT fire):
  - bare install / sync (no --auto*)
  - install --auto=keep-live
  - sync --auto=keep-tracked

Cross-cutting:
  - install --auto=use-tracked + --yes revert roundtrip
  - empty-drift no-op (no confirm fires)

PTY-driven tests rely on prompt_toolkit's full-screen Application rendering.
They live alongside the non-TTY guard tests so the regression set covers
both interactive and scripted ergonomics.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

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
# install --auto=use-tracked (section reconcile path)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_auto_use_tracked_with_yes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--auto=use-tracked --yes: bypasses confirm, applies, prints revert hint."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = _install(
        c,
        "test-reconcile-sections",
        extra=["--auto=use-tracked", "--yes"],
    )
    assert "rule B (new in tracked)" in c.read_text(_LIVE_SHARED)
    assert "revert with: setforge revert" in result.stdout


@pytest.mark.xdist_group("docker_daemon")
def test_install_auto_use_tracked_pty_confirm_yes(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """PTY confirm-yes: select Yes, Tab to OK, Enter applies the mutation.

    Drives prompt_toolkit's full-screen radiolist via the pyte harness
    anchors on the dialog title + prompt text + the
    default-no marker rendered in the emulated screen, sends arrow-down
    to select ``Yes``, then Tab to move focus to the ``Ok`` button, then
    Enter to submit (radiolist's own Enter handler only updates the
    radio selection — submitting the dialog requires focus on the OK
    button per ``prompt_toolkit.shortcuts.dialogs.radiolist_dialog``).
    Asserts the post-confirm ``proceeding`` line lands in the display
    before the child exits 0.
    """
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-reconcile-sections",
            f"--config={CONFIG_FIXTURE}",
            "--auto=use-tracked",
        ],
        timeout=60.0,
    )
    # Radiolist dialog header + prompt text.
    session.expect_in_display("setforge install", timeout=30.0)
    session.expect_in_display("Proceed with the mutation above?", timeout=10.0)
    # Default-no marker: "(*) No" appears as the selected radio item.
    session.expect_in_display("(*) No", timeout=5.0)
    # Arrow-down moves the cursor; Enter on the radio commits the new
    # selection (prompt_toolkit's RadioList._handle_enter writes
    # current_value); Tab moves focus to the OK button; Enter submits.
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    # The Yes radio is now committed.
    session.expect_in_display("(*) Yes", timeout=5.0)
    session.send_keys("\t")
    session.send_keys("\r")
    # Post-confirm "proceeding" line surfaces after the dialog exits.
    session.expect_in_display("proceeding", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    # Post-condition: mutation applied → tracked-side rule B landed.
    assert "rule B (new in tracked)" in c.read_text(_LIVE_SHARED)


@pytest.mark.xdist_group("docker_daemon")
def test_install_auto_use_tracked_pty_confirm_no(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """PTY confirm-no: Tab to OK + Enter accepts the default-No, aborts cleanly.

    Drives the same radiolist via the pyte harness:
    leaves the radio on its default ``No`` selection (default=False per
    ``confirm_auto_operation``), Tabs from the radiolist to the ``Ok``
    button, then Enter submits — the dialog returns False, which
    ``confirm_auto_operation`` maps to the ``aborted`` post-confirm
    line and a clean exit 0 with the live file untouched.
    """
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    pre = c.read_text(_LIVE_SHARED)
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "install",
            "--profile=test-reconcile-sections",
            f"--config={CONFIG_FIXTURE}",
            "--auto=use-tracked",
        ],
        timeout=60.0,
    )
    session.expect_in_display("setforge install", timeout=30.0)
    session.expect_in_display("Proceed with the mutation above?", timeout=10.0)
    session.expect_in_display("(*) No", timeout=5.0)
    # Default radio is No — Tab to focus OK, Enter to submit.
    session.send_keys("\t")
    session.send_keys("\r")
    session.expect_in_display("aborted", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    # Post-condition: live untouched.
    assert c.read_text(_LIVE_SHARED) == pre


def test_install_auto_use_tracked_non_tty_no_yes_exit_1(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-TTY + --auto=use-tracked without --yes → exit 1."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    result = _install(
        c,
        "test-reconcile-sections",
        extra=["--auto=use-tracked"],
        check=False,
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "--yes" in combined


# ---------------------------------------------------------------------------
# install --auto-accept-tracked (unexpected drift, tracked direction)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_auto_accept_tracked_with_yes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--auto-accept-tracked --yes: applies, exit 0."""
    c = docker_container()
    # Use jsonc-shallow (disposition: forked); replacing the whole live file
    # with an unrelated key produces unexpected drift the gate must surface.
    _install(c, "test-jsonc-shallow")
    # Mutate live to introduce unexpected drift.
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
    # Acceptance: didn't get blocked by the confirm gate.
    assert result.returncode == 0, result.stderr or result.stdout
    # Positive content check — confirms the install actually executed
    # past the gate (revert hint only prints on successful transition).
    assert "↩  revert with" in result.stdout


def test_install_auto_accept_tracked_non_tty_no_yes_exit_1(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-TTY + --auto-accept-tracked without --yes → exit 1."""
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
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "--yes" in combined


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
    assert "↩  revert with" in result.stdout


def test_install_auto_accept_live_non_tty_no_yes_exit_1(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Non-TTY + --auto-accept-live without --yes → exit 1."""
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
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "--yes" in combined


# ---------------------------------------------------------------------------
# sync --auto=use-live (capture)
# ---------------------------------------------------------------------------


def test_sync_auto_use_live_with_yes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """sync --auto=use-live --yes: captures, prints revert hint."""
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
def test_install_auto_use_tracked_revert_roundtrip(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Apply with --yes, then revert restores the original state."""
    c = docker_container()
    _install(c, "test-reconcile-sections")
    old = "- rule A\n"
    c.write_text(_LIVE_SHARED, _shared_section(old, _sha256(old)))
    pre = c.read_text(_LIVE_SHARED)
    _install(c, "test-reconcile-sections", extra=["--auto=use-tracked", "--yes"])
    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-reconcile-sections",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ],
        check=False,
    )
    assert revert.returncode == 0, revert.stderr or revert.stdout
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
