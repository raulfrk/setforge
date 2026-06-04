"""Docker e2e tests for the interactive disposition conflict wizard.

The hunk wizard (``setforge.conflict_wizard.make_wizard_resolver``) fires
during ``install`` on a ``disposition: shared|forked`` file ONLY when all of:

* ``--reconcile-user-sections`` is passed (the interactive switch), AND
* stdout is a TTY, AND
* ``--auto`` is NOT set (``--auto`` resolves every conflict non-interactively).

Per genuine conflict it renders ``ours (live)`` vs ``theirs (tracked)`` and
reads ONE keypress via :func:`setforge.wizard.read_one_choice`:

* ``k`` keep-yours  → live line wins, base advances.
* ``t`` take-upstream → tracked line wins, base advances.
* ``s`` skip        → live kept, base NOT advanced (conflict re-detected next run).
* ``e`` edit        → ``$EDITOR`` (not driven here — needs an interactive editor).

Harness choice
--------------
The wizard renders through a Rich ``Console`` and prompts with the raw-mode
single-keypress :func:`read_one_choice` (same primitive the shared user-section
wizard uses). The prompt line carries Rich-styled separators/colours, so the
``"Choice (k/t/e/s)"`` marker can land split across cursor-positioned bytes in
the raw stream. Per the project's e2e conventions for prompt panels we drive
the interactive scenarios through :func:`pyte_pty_session`, anchoring on the
EMULATED screen (``.display``) rather than the raw pexpect byte stream. The
``--auto`` bypass scenario emits no TUI at all, so it uses the plain
``c.exec`` non-interactive path.

Conflict recipe (mirrors ``test_e2e_docker_disposition.py``)
------------------------------------------------------------
Install once with the bare profile → seeds the stored base from tracked bytes
(live == tracked == base). Then edit the SAME middle line in BOTH the live
file and the tracked source so the 3-way merge produces a genuine same-line
conflict, and run the interactive install to drive the wizard.

Reuses the ``test-disposition-shared`` profile + ``disposition/note.md``
fixture already declared in ``tests/fixtures/e2e/setforge.test.yaml`` — no new
fixture is added.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-disposition-shared"

# Tracked source inside the container workspace; editing it simulates an
# upstream change.
_TRACKED_MD = "/workspace/tests/fixtures/e2e/tracked/disposition/note.md"
# Live destination for the shared-disposition file.
_LIVE_SHARED = "/home/tester/.setforge_e2e/disposition/shared.md"

# The canonical tracked body (matches the fixture src on disk).
_TRACKED_MD_BODY = "# Disposition fixture\n\nintro line\nmiddle line\nfooter line\n"

# Same-line divergence: both sides rewrite the middle line differently.
_LIVE_CONFLICT = "# Disposition fixture\n\nintro line\nmiddle-LIVE\nfooter line\n"
_TRACKED_CONFLICT = "# Disposition fixture\n\nintro line\nmiddle-TRACKED\nfooter line\n"

_INSTALL_CMD = [
    "uv",
    "run",
    "setforge",
    "install",
    f"--profile={_PROFILE}",
    f"--config={CONFIG_FIXTURE}",
]


def _bare_install(c: ContainerHandle) -> None:
    """Run a bare ``install`` to seed the stored base from tracked bytes."""
    result = c.exec(_INSTALL_CMD, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def _seed_same_line_conflict(c: ContainerHandle) -> None:
    """Seed base, then diverge live + tracked on the SAME middle line."""
    _bare_install(c)
    # Sanity: first install deployed tracked bytes verbatim.
    assert c.read_text(_LIVE_SHARED) == _TRACKED_MD_BODY
    c.write_text(_LIVE_SHARED, _LIVE_CONFLICT)
    c.write_text(_TRACKED_MD, _TRACKED_CONFLICT)


def _drive_wizard(
    pyte_pty_session: Callable[..., PyteSession],
    c: ContainerHandle,
    keypress: str,
) -> PyteSession:
    """Spawn the interactive install under pyte, drive ONE wizard choice.

    Anchors on the ``"Choice"`` prompt marker in the emulated display,
    sends ``keypress``, then waits for the install to exit 0.
    """
    session = pyte_pty_session(
        container=c.cid,
        cmd=[*_INSTALL_CMD, "--reconcile-user-sections"],
        timeout=120.0,
    )
    # The wizard renders the conflict block then prompts "Choice (k/t/e/s):".
    session.expect_in_display("Choice", timeout=60.0)
    session.send_keys(keypress)
    session.wait_for_exit(timeout=60, expected_code=0)
    return session


# ---------------------------------------------------------------------------
# 1: keep-yours (k) — live line wins.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_conflict_wizard_keep_yours_keeps_live(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """k: the conflict prompt appears; 'k' keeps the live middle line."""
    c = docker_container()
    _seed_same_line_conflict(c)
    _drive_wizard(pyte_pty_session, c, "k")
    live = c.read_text(_LIVE_SHARED)
    assert "middle-LIVE" in live, live
    assert "middle-TRACKED" not in live, live


# ---------------------------------------------------------------------------
# 2: take-upstream (t) — tracked line wins.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_conflict_wizard_take_upstream_takes_tracked(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """t: the conflict prompt appears; 't' takes the tracked middle line."""
    c = docker_container()
    _seed_same_line_conflict(c)
    _drive_wizard(pyte_pty_session, c, "t")
    live = c.read_text(_LIVE_SHARED)
    assert "middle-TRACKED" in live, live
    assert "middle-LIVE" not in live, live


# ---------------------------------------------------------------------------
# 3: skip (s) — live kept AND base NOT advanced (drift still reported).
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_conflict_wizard_skip_keeps_live_and_defers(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """s: 's' keeps live; the base is NOT advanced so compare still drifts.

    Skip defers re-baselining: the stored base stays at the original tracked
    bytes, so a follow-up ``compare`` still classifies the file as drifted
    (live diverged from tracked, conflict re-detectable on the next install).
    """
    c = docker_container()
    _seed_same_line_conflict(c)
    _drive_wizard(pyte_pty_session, c, "s")
    live = c.read_text(_LIVE_SHARED)
    assert "middle-LIVE" in live, live
    assert "middle-TRACKED" not in live, live

    # Base was NOT advanced → live still diverges from tracked → compare
    # reports drift (exit 1 under --check).
    compare = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "compare",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--check",
        ],
        check=False,
    )
    assert compare.returncode != 0, (
        f"compare --check unexpectedly clean after skip: "
        f"stdout={compare.stdout!r} stderr={compare.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 4: --auto bypasses the wizard entirely (no TUI; plain non-interactive run).
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_conflict_wizard_auto_bypasses_wizard(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """--auto=use-tracked resolves the conflict non-interactively — no prompt.

    With ``--auto`` set the resolver is never built (``_build_conflict_resolver``
    short-circuits), so no ``Choice`` prompt is emitted and the tracked value is
    taken. Driven via the plain non-interactive ``c.exec`` path (no TTY, no
    full-screen panel), with a timeout guard so a regression that DID prompt
    would block-and-fail rather than hang.
    """
    c = docker_container()
    _seed_same_line_conflict(c)
    result = c.exec(
        [
            "timeout",
            "60",
            *_INSTALL_CMD,
            "--auto=use-tracked",
        ],
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    combined = result.stdout + result.stderr
    # No interactive prompt was rendered.
    assert "Choice (" not in combined, combined
    # Tracked value taken non-interactively.
    live = c.read_text(_LIVE_SHARED)
    assert "middle-TRACKED" in live, live
    assert "middle-LIVE" not in live, live
