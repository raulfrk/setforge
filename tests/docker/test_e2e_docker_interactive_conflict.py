"""Real-TTY Docker e2e for interactive install conflict resolution.

A 3-way conflict (base vs a host live-edit vs an upstream tracked-edit on the
SAME line) drives ``install --reconcile-user-sections`` into the reconcile
engine's per-region conflict wizard (:func:`setforge.reconcile.wizard.resolve_conflicts`
— ``Ours · Theirs · [Edit] · Claude-merge · Skip``). The user picks a side and
the resolved bytes land on disk.

This is the layer the disposition/spans cutover removed: ``cb16d07`` deleted the
old ``test_e2e_docker_conflict_wizard.py`` and no full-flow TTY e2e replaced it.
``tests/test_install_wizard.py`` covers the install→reconcile WIRING but scripts
``reconcile_apply.resolve_conflicts`` (the real prompt_toolkit render never runs).
This test closes that gap: the genuine wizard renders under a real PTY and
accepts keystrokes.

Harness: the pyte-emulated PTY (:class:`tests.docker.pyte_session.PyteSession`) —
the full-screen prompt_toolkit ``Application`` splashes its frame across
cursor-positioned bytes, so we anchor on the EMULATED SCREEN (``.display``),
never the raw byte stream. The framed corner glyph (``┌``) is the render fence
we wait on before sending keys. Arrow-right is ``\\x1b[C``; Enter is ``\\r``
(carriage return, not ``\\n``) — see the pyte_session docstring.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

# A profile with a single PLAIN (line-unit) reconcile file — the classic 3-way
# conflict surface for the per-region wizard.
_PROFILE = "test-minimal"
_TRACKED = "/workspace/tests/fixtures/e2e/tracked/minimal/text.txt"
_LIVE = "/home/tester/.setforge_e2e/minimal/text.txt"

# The conflict recipe (mirrors tests/test_install_wizard.py::_seed_conflict):
# install seeds base = _BASE; the host diverges line 2 one way, upstream the
# other → a single conflicting region the wizard must resolve.
_BASE = "one\ntwo\nthree\n"
_LIVE_EDIT = "one\ntwo-HOST\nthree\n"
_UPSTREAM_EDIT = "one\ntwo-UPSTREAM\nthree\n"

# The framed corner glyph from the wizard's top rule: the render fence we wait
# on before sending input (the title could split across cursor moves, but the
# corner paints as one cell).
_FRAME_GLYPH = "┌"


def _prof(c: ContainerHandle, verb: str, *extra: str) -> tuple[int, str, str]:
    """Run a non-interactive ``setforge <verb>`` against the e2e profile."""
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            verb,
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            *extra,
        ],
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def _seed_conflict(c: ContainerHandle) -> None:
    """First-install to seed the base, then diverge both sides on line 2."""
    c.write_text(_TRACKED, _BASE)
    rc, _out, err = _prof(
        c, "install", "--no-secrets-scan", "--no-transition", "--no-git-check", "--yes"
    )
    assert rc == 0, err
    assert c.read_text(_LIVE) == _BASE
    c.write_text(_LIVE, _LIVE_EDIT)  # host edit
    c.write_text(_TRACKED, _UPSTREAM_EDIT)  # upstream edit → conflict on line 2


def _conflict_session(
    pyte_pty_session: Callable[..., PyteSession], c: ContainerHandle
) -> PyteSession:
    """Spawn an interactive ``install --reconcile-user-sections`` over a PTY."""
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
        timeout=120.0,
    )


@pytest.mark.xdist_group("docker_daemon")
def test_interactive_conflict_take_theirs_writes_upstream(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Choosing ``Theirs`` folds the upstream tracked bytes into the live file."""
    c = docker_container()
    _seed_conflict(c)

    s = _conflict_session(pyte_pty_session, c)
    s.expect_in_display(_FRAME_GLYPH)  # the wizard frame rendered
    s.expect_in_display("Theirs")  # the per-region button bar is up
    s.send_keys("\x1b[C\r")  # focus Ours(0) → Theirs(1), Enter
    s.wait_for_exit(timeout=60, expected_code=0)

    assert c.read_text(_LIVE) == _UPSTREAM_EDIT
    assert "two-HOST" not in c.read_text(_LIVE)


@pytest.mark.xdist_group("docker_daemon")
def test_interactive_conflict_keep_ours_keeps_host(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Choosing ``Ours`` (focused first) keeps the host live-edit verbatim."""
    c = docker_container()
    _seed_conflict(c)

    s = _conflict_session(pyte_pty_session, c)
    s.expect_in_display(_FRAME_GLYPH)
    s.expect_in_display("Ours")
    s.send_keys("\r")  # Ours is focused first → Enter selects it
    s.wait_for_exit(timeout=60, expected_code=0)

    assert c.read_text(_LIVE) == _LIVE_EDIT
    assert "two-UPSTREAM" not in c.read_text(_LIVE)
