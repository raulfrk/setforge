"""Docker e2e for the A5b structured (YAML) ``setforge stage`` walk.

A structured tracked_file stages per-KEY (dotted-path units), so the walk renders
the SAME full-screen ``button_bar`` per key — Share / Keep local / Skip / Quit,
with a Share sub-menu of Draft (Claude) / Verbatim / Skip. We drive it through the
pyte-emulated PTY (:class:`PyteSession`) and anchor on the EMULATED screen, never
the raw byte stream. This is the real-TTY guard the unit tests cannot give: their
``button_bar`` is monkeypatched, so only this e2e actually CONSTRUCTS the
structured Share sub-menu + the ``draft_key_unit`` review widgets (the class of
widget-construction crash a callback-injected test misses).

Profile ``test-stage-structured`` (``tests/fixtures/e2e/setforge.test.yaml``)
declares one YAML tracked_file ``stage_settings`` (``stage/settings.yaml``). The
journey: ``install`` seeds the merge base, a live edit changes two keys (a
shareable ``theme`` + a host-specific ``workdir``), the walk classifies them per
key, ``sync`` promotes only the SHARED key into tracked, and a demote reverts it.
A second journey drafts the host-specific ``workdir`` value into a type-confined
shareable scalar via a stubbed ``claude``.

pyte conventions (see the pyte_session docstring): arrow-right ``\\x1b[C``,
arrow-left ``\\x1b[D`` (focus wraps), Enter ``\\r`` (NOT ``\\n``). Focus is
index-based and deterministic. Keys sort by dotted path, so ``theme`` precedes
``workdir``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-stage-structured"
_TRACKED = "/workspace/tests/fixtures/e2e/tracked/stage/settings.yaml"
_LIVE = "/home/tester/.setforge_e2e/stage/settings.yaml"
_FAKE_CLAUDE = "/home/tester/fake-claude"

# The fixture base (== tracked src on disk and the live bytes right after install).
_BASE_BODY = (
    "# editor settings (structured per-key staging e2e fixture)\n"
    "theme: dark\n"
    "workdir: /home/generic\n"
)
# Live edit: a shareable theme change + a host-specific workdir change. Two
# independent per-key units (sorted: theme, then workdir).
_LIVE_BODY = (
    "# editor settings (structured per-key staging e2e fixture)\n"
    "theme: light\n"
    "workdir: /home/tester\n"
)
# Draft-journey live edit: change ONLY the host-specific workdir → exactly one
# per-key unit, so the post-sync SHARED_DRAFTED divergence is the only drift and
# `compare --check` stays clean (an unstaged PENDING theme would be raw drift).
_LIVE_DRAFT_BODY = (
    "# editor settings (structured per-key staging e2e fixture)\n"
    "theme: dark\n"
    "workdir: /home/tester\n"
)

# The stub emits a bare shareable SCALAR (no `key:`), so it parses as a single
# string of the SAME type as the host workdir — a `key: value` line would parse
# to a map and be rejected by the type-confinement gate.
_DRAFT_SCALAR = "~/projects"
_FAKE_CLAUDE_SCRIPT = f"#!/bin/sh\ncat >/dev/null\nprintf '{_DRAFT_SCALAR}\\n'\n"

_FRAME_GLYPH = "┌"  # the bar's top-rule corner; the render fence.


def _setforge(c: ContainerHandle, args: list[str]) -> tuple[int, str, str]:
    result = c.exec(["uv", "run", "setforge", *args], check=False)
    return result.returncode, result.stdout, result.stderr


def _prof(c: ContainerHandle, verb: str, *extra: str) -> tuple[int, str, str]:
    return _setforge(
        c, [verb, f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}", *extra]
    )


def _stage_session(
    pyte_pty_session: Callable[..., PyteSession], c: ContainerHandle
) -> PyteSession:
    """Spawn an interactive ``setforge stage stage_settings`` over the pyte PTY."""
    return pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "stage",
            "stage_settings",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
        timeout=120.0,
    )


@pytest.mark.xdist_group("docker_daemon")
def test_structured_walk_shares_one_key_then_demotes(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Share (verbatim) the ``theme`` key + keep ``workdir`` local → sync promotes
    only the shared key into tracked; a demote re-stage reverts it (INV-8)."""
    c = docker_container()
    assert _prof(c, "install")[0] == 0
    c.write_text(_LIVE, _LIVE_BODY)

    # --- walk 1: Share→Verbatim theme; Keep-local workdir.
    s = _stage_session(pyte_pty_session, c)
    s.expect_in_display(_FRAME_GLYPH, timeout=60.0)
    s.expect_in_display("theme", timeout=30.0)  # key 1 = theme (sorted first)
    s.send_keys("\r")  # Share (focused) → opens the structured share sub-menu
    s.expect_in_display("Verbatim", timeout=30.0)  # sub-menu CONSTRUCTED + rendered
    s.send_keys("\x1b[C\r")  # focus 0(Draft)→1 = Verbatim → Decision(SHARED)
    s.expect_in_display("workdir", timeout=30.0)  # key 2 = workdir
    s.send_keys("\x1b[C\r")  # focus 0→1 = Keep local → Decision(LOCAL)
    s.expect_in_display("1 shared", timeout=30.0)  # summary: 1 shared  1 local
    s.wait_for_exit(timeout=60, expected_code=0)

    assert _prof(c, "sync", "-y")[0] == 0
    tracked = c.read_text(_TRACKED)
    assert "theme: light" in tracked  # SHARED key promoted
    assert "workdir: /home/generic" in tracked  # LOCAL key held at base value
    assert "/home/tester" not in tracked  # host value never leaked upstream
    assert "/home/tester" in c.read_text(_LIVE)  # live keeps the host value

    # --- walk 2: demote the now-SHARED theme back to LOCAL.
    s2 = _stage_session(pyte_pty_session, c)
    s2.expect_in_display(_FRAME_GLYPH, timeout=60.0)
    s2.expect_in_display("theme", timeout=30.0)
    s2.send_keys("\x1b[C\r")  # theme: focus 0→1 = Keep local (demote)
    s2.expect_in_display("workdir", timeout=30.0)
    s2.send_keys("\r")  # workdir: Keep local already focused → keep
    s2.expect_in_display("0 shared", timeout=30.0)  # summary: 0 shared  2 local
    s2.wait_for_exit(timeout=60, expected_code=0)

    assert _prof(c, "sync", "-y")[0] == 0
    # Both keys now LOCAL → tracked is byte-restored to the base fixture.
    assert c.read_text(_TRACKED) == _BASE_BODY


@pytest.mark.xdist_group("docker_daemon")
def test_structured_walk_draft_type_confined_scalar(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Draft the host-specific ``workdir`` value into a shareable scalar: the
    stubbed rewrite is type-confined (same str type) and promoted on sync, while
    the host value stays live (a blessed SHARED_DRAFTED divergence)."""
    c = docker_container(env={"SETFORGE_CLAUDE_BIN": _FAKE_CLAUDE})
    c.write_text(_FAKE_CLAUDE, _FAKE_CLAUDE_SCRIPT)
    c.exec(["chmod", "+x", _FAKE_CLAUDE], check=True)
    assert _prof(c, "install")[0] == 0
    c.write_text(_LIVE, _LIVE_DRAFT_BODY)  # only workdir changed → one key-unit

    s = _stage_session(pyte_pty_session, c)
    s.expect_in_display(_FRAME_GLYPH, timeout=60.0)
    s.expect_in_display("workdir", timeout=30.0)  # the lone key = workdir
    s.send_keys("\r")  # Share (focused) → structured share sub-menu
    s.expect_in_display("Verbatim", timeout=30.0)
    s.send_keys("\r")  # Draft (Claude) (focused) → instruction prompt
    s.expect_in_display("guidance", timeout=30.0)  # the instruction hint
    s.send_keys("\r")  # empty instruction → let the (stubbed) model draft
    s.expect_in_display("projects", timeout=60.0)  # the type-confined scalar, in review
    s.send_keys("\r")  # Keep mine local (default focus) → SHARED_DRAFTED, no adopt
    s.expect_in_display("1 drafted", timeout=30.0)
    s.wait_for_exit(timeout=60, expected_code=0)

    assert _prof(c, "sync", "-y")[0] == 0
    tracked = c.read_text(_TRACKED)
    assert f"workdir: {_DRAFT_SCALAR}" in tracked  # the drafted scalar promoted
    assert "/home/tester" not in tracked  # host value NEVER leaked into tracked
    assert "/home/tester" in c.read_text(_LIVE)  # live keeps the host value

    assert _prof(c, "compare", "--check")[0] == 0

    c.write_text(_TRACKED, c.read_text(_TRACKED).replace(_DRAFT_SCALAR, "~/tampered"))
    assert _prof(c, "compare", "--check")[0] != 0
