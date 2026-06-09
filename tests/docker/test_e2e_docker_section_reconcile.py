"""Docker E2E tests for the shared-span reconcile surface (2.0 contract).

Under the 1.2 -> 2.0 contract the legacy marker-body three-way section
reconcile is gone: a shared span carries pure INTENT (anchor / kind /
semantics) on the tracked-side ``setforge.yaml`` ``spans`` block and has no
tracked body to 3-way merge. The only thing left to reconcile is an INTENT
COLLISION — a host-local span declared in ``local.yaml`` that shadows a
shared span on the SAME anchor. ``setforge.config.detect_shared_span_collisions``
surfaces it and the install path (``_reconcile_shared_spans``) gates it.

Routing matrix exercised end-to-end against a fresh Debian 12 container:

- bare install: silent host-local-wins, no nag.
- ``--auto=use-tracked``: every collision resolves to the shared intent AND
  a per-collision "host-local span ... overwritten" risk line prints.
- ``--auto=keep-live``: host-local override kept, no risk line.
- ``--reconcile-user-sections`` non-tty: refuses (require-interactive).
- ``--reconcile-user-sections`` + tty: per-collision radiolist confirm
  (driven via the pyte PTY harness); Yes adopts shared, No keeps host-local.
- ``--reconcile-user-sections`` + ``--auto``: mutually exclusive, exit 2.

The collision fixture rides ``spans_pinned_md`` (a shared span on
``## Pinned Section``); the host-local shadow is declared at test time in
``local.yaml``. The baseline install runs WITHOUT the shadow so the
``--auto`` "drift exists" precondition holds, then the shadow is added and
the reconcile mode is exercised.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-spans-pinned"
_TRACKED_FILE = "spans_pinned_md"
_ANCHOR = "## Pinned Section"
_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_LIVE = "/home/tester/.setforge_e2e/spans/pinned.md"

# local.yaml WITHOUT the host-local shadow — the baseline install source.
_LOCAL_BASE = "source:\n  kind: path\n  path: /workspace/tests/fixtures/e2e\n"

# local.yaml WITH a host-local span on the SAME anchor as the tracked-side
# shared span — the intent collision the reconcile path surfaces.
_LOCAL_COLLISION = (
    "source:\n"
    "  kind: path\n"
    "  path: /workspace/tests/fixtures/e2e\n"
    "tracked_files:\n"
    f"  {_TRACKED_FILE}:\n"
    "    spans:\n"
    f'      - anchor: "{_ANCHOR}"\n'
    "        kind: forked\n"
    "        semantics: host-local\n"
)


def _install(
    c: ContainerHandle,
    *,
    extra: list[str] | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """Run ``setforge install`` for the span-collision profile."""
    cmd = [
        "uv",
        "run",
        "setforge",
        "install",
        f"--profile={_PROFILE}",
        f"--config={CONFIG_FIXTURE}",
    ]
    if extra:
        cmd.extend(extra)
    if timeout is not None:
        cmd = ["timeout", str(timeout), *cmd]
    result = c.exec(cmd, check=False)
    if check:
        assert result.returncode == 0, result.stderr or result.stdout
    return result.returncode, result.stdout, result.stderr


def _seed_baseline(c: ContainerHandle) -> None:
    """Install once WITHOUT the collision so a later --auto run has drift."""
    c.write_text(_LOCAL_YAML, _LOCAL_BASE)
    _install(c)
    # Now declare the host-local shadow that collides with the shared span.
    c.write_text(_LOCAL_YAML, _LOCAL_COLLISION)


# ---------------------------------------------------------------------------
# Deterministic flag matrix
# ---------------------------------------------------------------------------


def test_install_bare_collision_no_nag(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Bare install on a span collision keeps silent host-local-wins, no nag."""
    c = docker_container()
    _seed_baseline(c)
    _rc, stdout, stderr = _install(c)
    combined = stdout + stderr
    assert "host-local span" not in combined, combined
    assert "overwritten" not in combined, combined


def test_install_auto_use_tracked_surfaces_risk_line(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--auto=use-tracked adopts the shared intent and prints a risk line."""
    c = docker_container()
    _seed_baseline(c)
    _rc, stdout, stderr = _install(c, extra=["--auto=use-tracked", "--yes"])
    combined = stdout + stderr
    assert "host-local span" in combined, combined
    assert _ANCHOR in combined, combined
    assert "overwritten" in combined, combined


def test_install_auto_keep_live_keeps_host_local_quietly(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--auto=keep-live keeps the host-local override; no overwrite risk line."""
    c = docker_container()
    _seed_baseline(c)
    _rc, stdout, stderr = _install(c, extra=["--auto=keep-live", "--yes"])
    combined = stdout + stderr
    assert "overwritten" not in combined, combined


def test_install_reconcile_non_tty_refuses(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--reconcile-user-sections + non-tty + a collision refuses cleanly."""
    c = docker_container()
    _seed_baseline(c)
    rc, stdout, stderr = _install(c, extra=["--reconcile-user-sections"], check=False)
    assert rc != 0, stdout + stderr
    combined = stdout + stderr
    assert "not a TTY" in combined or "collision" in combined, combined


def test_install_mutually_exclusive_flags_exit_2(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--reconcile-user-sections + --auto=... exits 2."""
    c = docker_container()
    c.write_text(_LOCAL_YAML, _LOCAL_BASE)
    rc, stdout, stderr = _install(
        c,
        extra=["--reconcile-user-sections", "--auto=use-tracked"],
        check=False,
    )
    assert rc == 2, stdout + stderr
    assert "mutually exclusive" in (stdout + stderr)


def test_install_no_collision_reconcile_exits_silently(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--reconcile-user-sections + no collision exits 0 without hanging.

    Timeout guard catches a wizard that wrongly tries to prompt when there is
    no collision to resolve.
    """
    c = docker_container()
    c.write_text(_LOCAL_YAML, _LOCAL_BASE)
    rc, stdout, stderr = _install(
        c, extra=["--reconcile-user-sections"], timeout=30, check=False
    )
    assert rc == 0, stderr
    combined = stdout + stderr
    assert "host-local span" not in combined, combined


def test_install_auto_use_tracked_then_revert_restores_live(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A --auto=use-tracked install is revertible back to the pre-install live."""
    c = docker_container()
    _seed_baseline(c)
    pre = c.read_text(_LIVE)
    _install(c, extra=["--auto=use-tracked", "--yes"])
    revert = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "revert",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--yes",
        ],
        check=False,
    )
    assert revert.returncode == 0, revert.stderr or revert.stdout
    assert c.read_text(_LIVE) == pre


# ---------------------------------------------------------------------------
# Interactive (tty) radiolist confirm — driven via the pyte PTY harness
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_install_reconcile_interactive_adopt_shared(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """PTY confirm Yes: the per-collision radiolist adopts the shared intent.

    ``--reconcile-user-sections`` on a collision renders the
    ``confirm_auto_operation`` radiolist (full-screen prompt_toolkit) per
    collision. Selecting Yes resolves toward the tracked-side shared intent;
    the post-confirm ``proceeding`` line surfaces before a clean exit 0.
    """
    c = docker_container()
    _seed_baseline(c)
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--reconcile-user-sections",
        ],
        timeout=60.0,
    )
    session.expect_in_display("install --reconcile-user-sections", timeout=30.0)
    session.expect_in_display("Proceed with the mutation above?", timeout=10.0)
    session.expect_in_display("(*) No", timeout=5.0)
    # Arrow-down to Yes, Enter commits the radio, Tab to OK, Enter submits.
    session.send_keys("\x1b[B")
    session.send_keys("\r")
    session.expect_in_display("(*) Yes", timeout=5.0)
    session.send_keys("\t")
    session.send_keys("\r")
    session.expect_in_display("proceeding", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)


@pytest.mark.xdist_group("docker_daemon")
def test_install_reconcile_interactive_keep_host_local(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """PTY confirm No: the radiolist default keeps the host-local override.

    Leaving the radio on its default No and submitting maps to the ``aborted``
    post-confirm line — the host-local span is kept and the install still
    exits 0 (the collision was resolved by keeping live).
    """
    c = docker_container()
    _seed_baseline(c)
    session = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--reconcile-user-sections",
        ],
        timeout=60.0,
    )
    session.expect_in_display("install --reconcile-user-sections", timeout=30.0)
    session.expect_in_display("Proceed with the mutation above?", timeout=10.0)
    session.expect_in_display("(*) No", timeout=5.0)
    # Default radio is No — Tab to OK, Enter submits.
    session.send_keys("\t")
    session.send_keys("\r")
    session.expect_in_display("aborted", timeout=15.0)
    session.wait_for_exit(timeout=60.0, expected_code=0)
