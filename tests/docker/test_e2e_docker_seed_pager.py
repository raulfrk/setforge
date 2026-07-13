"""Real-TTY Docker e2e for the first-install seed divergence-preview pager.

The seed prompt fires when a tracked file already exists in live, differs from
upstream, and has NO recorded merge base (base ABSENT → a 2-side live-vs-upstream
divergence). The enriched prompt carries a ``View diff`` button that opens an
in-app ``less``-style pager over the full diff; ``q`` returns to the button bar
with the choice intact, and Esc/Ctrl-C anywhere aborts the file (nothing written,
no base recorded — INV-1).

This closes the callback-injected blind spot: the unit tests script the pager +
button-bar headlessly, but only a real PTY exercises the genuine prompt_toolkit
render + keystroke path (a prior UI bug shipped a style crash the scripted tests
could not see). It also exercises terminal RESIZE (:meth:`PyteSession.resize`)
so the pager survives a SIGWINCH shrink.

Harness notes mirror ``test_e2e_docker_interactive_conflict``: anchor on the
pyte-emulated screen (``.display``), wait on the framed corner glyph (``┌``)
before sending keys, arrows are ``\\x1b[C`` / Enter is ``\\r``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-minimal"
_TRACKED = "/workspace/tests/fixtures/e2e/tracked/minimal/text.txt"
_LIVE = "/home/tester/.setforge_e2e/minimal/text.txt"

# A live file (no base recorded) that diverges from upstream over MANY lines so
# the pager has content to scroll past a small terminal window.
_LIVE_BODY = "".join(f"live-line-{i}\n" for i in range(60))
_UPSTREAM_BODY = "".join(f"upstream-line-{i}\n" for i in range(60))

_FRAME_GLYPH = "┌"


def _seed_session(
    pyte_pty_session: Callable[..., PyteSession],
    c: ContainerHandle,
    *,
    cols: int = 120,
    lines: int = 40,
) -> PyteSession:
    """Spawn an interactive ``install --reconcile-user-sections`` over a PTY.

    No prior install has run, so the tracked file's base is ABSENT and the
    divergent (already-present) live file trips the seed prompt.
    """
    return pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--reconcile-user-sections",
            "--no-secrets-scan",
            "--no-transition",
            "--no-git-check",
        ],
        cols=cols,
        lines=lines,
        timeout=120.0,
    )


def _seed_divergence(c: ContainerHandle) -> None:
    """Place a divergent live file + differing upstream with NO recorded base."""
    c.write_text(_LIVE, _LIVE_BODY)
    c.write_text(_TRACKED, _UPSTREAM_BODY)


@pytest.mark.xdist_group("docker_daemon")
def test_seed_pager_view_scroll_resize_then_choose(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """v opens the pager; scroll + a resize survive; q returns; a choice lands.

    Asserts ``.display`` in three states: (1) the seed button bar with the
    ``View diff`` affordance + divergence summary, (2) the pager scrolled to
    the bottom under a SHRUNKEN terminal, (3) back at the button bar after
    ``q``, then Take upstream folds the upstream bytes into live.
    """
    c = docker_container()
    _seed_divergence(c)

    s = _seed_session(pyte_pty_session, c, cols=120, lines=40)
    # State 1: the seed button bar with the summary + the view-diff button.
    s.expect_in_display(_FRAME_GLYPH)
    s.expect_in_display("live vs upstream")
    s.expect_in_display("View diff")

    # Open the pager: the diff renders every live (-) row first, then every
    # upstream (+) row, so the top page shows the live side.
    s.send_keys("v")
    s.expect_in_display("-live-line-0")
    # Jump to the bottom, then SHRINK the terminal — the offset must re-clamp
    # (no crash, the last upstream row stays reachable).
    s.send_keys("G")  # jump to last page
    s.expect_in_display("+upstream-line-59")
    s.resize(cols=100, lines=12)
    s.send_keys("G")  # re-anchor to the bottom at the new (shorter) size
    # State 2: still showing the tail after the shrink (clamped, not crashed).
    s.expect_in_display("+upstream-line-59")

    # State 3: q returns to the button bar (restore a roomy size first so the
    # bar is not cramped) with the choice still open.
    s.send_keys("q")
    s.resize(cols=120, lines=40)
    s.expect_in_display("View diff")
    s.send_keys("t")  # 't' is the derived accelerator for "Take upstream"
    s.wait_for_exit(timeout=60, expected_code=0)

    assert c.read_text(_LIVE) == _UPSTREAM_BODY


@pytest.mark.xdist_group("docker_daemon")
def test_seed_pager_escape_aborts_file_unchanged(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Esc inside the pager aborts: live UNCHANGED, no base recorded (INV-1)."""
    c = docker_container()
    _seed_divergence(c)

    s = _seed_session(pyte_pty_session, c)
    s.expect_in_display(_FRAME_GLYPH)
    s.expect_in_display("View diff")
    s.send_keys("v")  # open the pager
    s.expect_in_display("-live-line-0")
    s.send_keys("\x1b")  # Esc in the pager → propagate CANCEL → abort the file
    # A cancelled seed leaves the install with nothing to do for this file; the
    # process exits cleanly (the file was untouched).
    s.wait_for_exit(timeout=60, expected_code=0)

    # The live file is EXACTLY what we placed — never overwritten with upstream.
    assert c.read_text(_LIVE) == _LIVE_BODY
    # No base was recorded, so a fresh interactive install re-offers the seed.
    s2 = _seed_session(pyte_pty_session, c)
    s2.expect_in_display("live vs upstream")
    s2.send_keys("\r")  # Keep live (first button) to leave cleanly
    s2.wait_for_exit(timeout=60, expected_code=0)
    assert c.read_text(_LIVE) == _LIVE_BODY
