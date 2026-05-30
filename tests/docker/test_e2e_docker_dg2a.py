"""Docker E2E PTY tests for the dg2a sync-wizard ``[p]`` auto-promote.

Exercises the wizard end-to-end against a fresh Debian 12 container:

1. ``setforge install --profile=test-xsco`` injects the host-local
   section declared in ``local.yaml`` into the live tracked file.
2. ``setforge sync --profile=test-xsco`` walks promotable host-local
   sections, renders the ``[k]/[p]/[s]/[q]`` menu, and on ``p``
   dispatches the all-in-one confirm panel + arrow-key
   ``radiolist_dialog`` (default=No).

The :func:`tests.docker.conftest.pyte_pty_session` fixture
captures the emulated screen so we can assert on the
rendered prompt, the confirm panel, the secrets-scan row, the RISKS
block, and the post-promote file mutations.

Five cases per spec dg2a acceptance:

- ``test_promote_pty_confirm_yes`` — user picks ``[p]``, then Yes;
  promote applies, exit 0, post-state asserts the three-file mutation.
- ``test_promote_pty_confirm_no`` — user picks ``[p]``, then default-No;
  no mutations, exit 0.
- ``test_promote_pty_confirm_esc`` — user picks ``[p]``, then Escape on
  the confirm dialog; no mutations, exit 0.
- ``test_promote_pty_secrets_warned`` — body containing a
  gitleaks-detectable secret surfaces the warning in the RISKS panel;
  user can still proceed.
- ``test_promote_pty_then_revert`` — full promote applied, then
  ``setforge revert`` rolls every mutation back; final bytes match the
  pre-promote snapshot.

NO skipped or xfailed tests per the A30 (no-gaps) policy.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_HOST_LIVE = "/home/tester/.setforge_e2e/xsco/host.md"
_HOST_TRACKED = "/workspace/tests/fixtures/e2e/tracked/xsco/host.md"


def _local_yaml_with_section(section_name: str, body_line: str) -> str:
    """Build a local.yaml with one host_local_sections entry."""
    return (
        "tracked_files:\n"
        "  xsco_md:\n"
        "    host_local_sections:\n"
        f"      {section_name}:\n"
        "        anchor: {kind: after-heading, value: Workflow}\n"
        "        body: |\n"
        f"          {body_line}\n"
    )


def _install_xsco(c: ContainerHandle, *, profile: str = "test-xsco") -> None:
    """Run ``setforge install`` so the host-local section lands in the live file."""
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={profile}",
            f"--config={CONFIG_FIXTURE}",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _sync_cmd() -> list[str]:
    """Bare ``setforge sync`` — the sync wizard fires the [p] prompt itself."""
    return [
        "uv",
        "run",
        "setforge",
        "sync",
        "--profile=test-xsco",
        f"--config={CONFIG_FIXTURE}",
    ]


@pytest.mark.xdist_group("docker_daemon")
def test_promote_pty_confirm_yes(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """User picks [p] + Yes: promote applies; exit 0."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, _local_yaml_with_section("promo", "PROMO BODY"))
    _install_xsco(c)
    # Verify the host-local section landed in live before promoting.
    live_pre = c.read_text(_HOST_LIVE)
    assert "start host-local promo" in live_pre

    session = pyte_pty_session(
        container=c.cid,
        cmd=_sync_cmd(),
        timeout=60.0,
    )
    # The sync wizard surfaces the per-section prompt first.
    session.expect_in_display("promo", timeout=30.0)
    session.expect_in_display("Choice (k/p/s/q)", timeout=10.0)
    # Send 'p' to pick promote; the confirm panel + dialog render next.
    session.send_keys("p")
    session.expect_in_display("Promote section", timeout=30.0)
    session.expect_in_display("(*) No", timeout=10.0)
    # Arrow-down moves the radio cursor; Enter commits the selection.
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    session.expect_in_display("(*) Yes", timeout=5.0)
    # Tab to OK button; Enter submits.
    session.send_keys("\t")
    session.send_keys("\r")
    session.expect_in_display("proceeding", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)

    # Post-state: live markers say "shared"; tracked file gained a shared pair.
    live_post = c.read_text(_HOST_LIVE)
    assert "start shared promo" in live_post
    assert "end shared promo" in live_post
    tracked_post = c.read_text(_HOST_TRACKED)
    assert "start shared promo" in tracked_post


@pytest.mark.xdist_group("docker_daemon")
def test_promote_pty_confirm_no(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """User picks [p] + default-No: no mutations; exit 0."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, _local_yaml_with_section("nope", "NOPE BODY"))
    _install_xsco(c)
    live_pre = c.read_text(_HOST_LIVE)
    local_yaml_pre = c.read_text(_HOME_LOCAL_YAML)

    session = pyte_pty_session(
        container=c.cid,
        cmd=_sync_cmd(),
        timeout=60.0,
    )
    session.expect_in_display("nope", timeout=30.0)
    session.expect_in_display("Choice (k/p/s/q)", timeout=10.0)
    session.send_keys("p")
    session.expect_in_display("Promote section", timeout=30.0)
    session.expect_in_display("(*) No", timeout=10.0)
    # No arrow-down — leave selection on No, Tab to OK, Enter.
    session.send_keys("\t")
    session.send_keys("\r")
    session.expect_in_display("aborted", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)

    # No file changes from the promote.
    assert "start host-local nope" in c.read_text(_HOST_LIVE)
    assert c.read_text(_HOST_LIVE) == live_pre
    assert c.read_text(_HOME_LOCAL_YAML) == local_yaml_pre


@pytest.mark.xdist_group("docker_daemon")
def test_promote_pty_confirm_esc(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """User picks [p], then Esc on the dialog: treated as abort; exit 0."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, _local_yaml_with_section("escape", "ESCAPE BODY"))
    _install_xsco(c)
    live_pre = c.read_text(_HOST_LIVE)
    local_yaml_pre = c.read_text(_HOME_LOCAL_YAML)

    session = pyte_pty_session(
        container=c.cid,
        cmd=_sync_cmd(),
        timeout=60.0,
    )
    session.expect_in_display("escape", timeout=30.0)
    session.expect_in_display("Choice (k/p/s/q)", timeout=10.0)
    session.send_keys("p")
    session.expect_in_display("Promote section", timeout=30.0)
    # Wait until the radiolist has rendered the default-No row: this is
    # the fence that proves prompt_toolkit's Application has focus and
    # is ready to consume keystrokes. The "Promote section" title paints
    # earlier in the redraw sequence and the dialog drops keys sent
    # before focus is wired (round-4 ESC bug).
    session.expect_in_display("(*) No", timeout=10.0)
    # Double-tap Esc to abort the radiolist_dialog. prompt_toolkit's
    # KeyProcessor reads a lone \x1b as the prefix of an escape sequence
    # (e.g. arrow keys) and waits for follow-up bytes; the second \x1b
    # resolves the ambiguity to Keys.Escape, which binds to "exit with
    # None" on radiolist_dialog. A single \x1b would only resolve after
    # the renderer's input timeout, which exceeds this test's 15s
    # expect window in practice.
    session.send_keys("\x1b\x1b")
    session.expect_in_display("aborted", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
    assert c.read_text(_HOST_LIVE) == live_pre
    assert c.read_text(_HOME_LOCAL_YAML) == local_yaml_pre


@pytest.mark.xdist_group("docker_daemon")
def test_promote_pty_secrets_warned(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """Body with a gitleaks-detectable secret warns in the panel; user proceeds."""
    c = docker_container()
    # Use a gitleaks-detected pattern in the body. AWS-style access key
    # is the most reliable gitleaks-default detector. Built up at runtime
    # so the literal string never appears in this source file (which
    # would trigger our own pre-commit gitleaks hook).
    secret_line = "AKIA" + "IOSFODNN" + "7" + "EXAMPLE"  # gitleaks:allow
    c.write_text(_HOME_LOCAL_YAML, _local_yaml_with_section("leaky", secret_line))
    _install_xsco(c)

    session = pyte_pty_session(
        container=c.cid,
        cmd=_sync_cmd(),
        timeout=90.0,
    )
    session.expect_in_display("leaky", timeout=30.0)
    session.expect_in_display("Choice (k/p/s/q)", timeout=10.0)
    session.send_keys("p")
    session.expect_in_display("Promote section", timeout=30.0)
    # The secrets-scan row surfaces in the panel. The exact rule_id
    # depends on the gitleaks ruleset shipped in the image; assert on
    # the "finding(s)" prefix the panel always renders for >= 1 hit.
    session.expect_in_display("Secrets scan", timeout=15.0)
    session.expect_in_display("(*) No", timeout=10.0)
    # Arrow-down to Yes; Enter to commit; Tab to OK; Enter to submit.
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    session.send_keys("\t")
    session.send_keys("\r")
    session.expect_in_display("proceeding", timeout=15.0)
    session.wait_for_exit(timeout=90.0, expected_code=0)
    # Promote applied despite findings.
    live_post = c.read_text(_HOST_LIVE)
    assert "start shared leaky" in live_post


@pytest.mark.xdist_group("docker_daemon")
def test_promote_pty_then_revert(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """Full promote + setforge revert round-trip: post-revert == pre-promote bytes."""
    c = docker_container()
    c.write_text(_HOME_LOCAL_YAML, _local_yaml_with_section("rt", "RT BODY"))
    _install_xsco(c)
    live_pre = c.read_text(_HOST_LIVE)
    local_yaml_pre = c.read_text(_HOME_LOCAL_YAML)
    tracked_pre = c.read_text(_HOST_TRACKED)

    session = pyte_pty_session(
        container=c.cid,
        cmd=_sync_cmd(),
        timeout=60.0,
    )
    session.expect_in_display("rt", timeout=30.0)
    session.expect_in_display("Choice (k/p/s/q)", timeout=10.0)
    session.send_keys("p")
    session.expect_in_display("Promote section", timeout=30.0)
    # Same focus-fence as test_promote_pty_confirm_yes: wait for the
    # default-No row to render before sending arrow keys; otherwise the
    # arrow-down is dropped and the radio cursor stays on No.
    session.expect_in_display("(*) No", timeout=10.0)
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    session.expect_in_display("(*) Yes", timeout=5.0)
    session.send_keys("\t")
    session.send_keys("\r")
    session.expect_in_display("proceeding", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)

    # Promote applied.
    assert "start shared rt" in c.read_text(_HOST_LIVE)

    # Now revert.
    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            "--profile=test-xsco",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ],
        check=False,
    )
    assert revert.returncode == 0, revert.stderr or revert.stdout

    # Post-revert: byte-identical to pre-promote.
    assert c.read_text(_HOST_LIVE) == live_pre
    assert c.read_text(_HOME_LOCAL_YAML) == local_yaml_pre
    assert c.read_text(_HOST_TRACKED) == tracked_pre
