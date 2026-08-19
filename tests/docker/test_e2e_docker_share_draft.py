"""Docker e2e for the A5c share auto-draft sub-flow (Claude rewrite on stage).

The draft sub-flow hands ONE host-specific hunk's live region to a resumable
``ClaudeSession`` for a shareable rewrite, then offers **Keep mine local**
(share the draft, keep host bytes live — a blessed ``live != tracked``
divergence) or **Adopt locally** (rewrite live to the draft too). We stub the
``claude`` binary with a deterministic script via ``SETFORGE_CLAUDE_BIN`` so the
rewrite is reproducible — the real binary's output is non-deterministic and
needs auth. The session resolves the override through
:func:`setforge.binaries.resolve_binary`; the scrubbed child env never sees the
``SETFORGE_*`` vars, so the stub just consumes stdin and prints the draft.

Two journeys over the plain ``stage_notes`` tracked_file (profile
``test-stage``): a host-specific ``workdir`` edit is drafted, then either kept
local (tracked gets the draft, live keeps the host path; ``compare`` stays
clean; ``revert`` restores) or adopted (live AND tracked become the draft).

pyte conventions: arrow-right ``\\x1b[C`` (focus wraps), Enter ``\\r`` (NOT
``\\n``); full-screen panels are anchored on the EMULATED screen.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-stage"
_TRACKED = "/workspace/tests/fixtures/e2e/tracked/stage/notes.md"
_LIVE = "/home/tester/.setforge_e2e/stage/notes.md"
_FAKE_CLAUDE = "/home/tester/fake-claude"

# The deterministic shareable rewrite the stub emits for the drafted region.
_DRAFT_MARKER = "workdir: SHAREABLE_DRAFT"

# Live edit: change ONLY the host-specific workdir line → exactly one diff hunk.
_LIVE_BODY = (
    "# Shareable notes\n\n"
    "## Tooling\n"
    "Use rg not grep; fd not find.\n\n"
    "## Host paths\n"
    "workdir: /home/tester\n"
)
# The stub ignores its args + prompt and prints the shareable draft line.
_FAKE_CLAUDE_SCRIPT = f"#!/bin/sh\ncat >/dev/null\nprintf '{_DRAFT_MARKER}\\n'\n"

_FRAME_GLYPH = "┌"


def _setforge(c: ContainerHandle, args: list[str]) -> tuple[int, str, str]:
    result = c.exec(["uv", "run", "setforge", *args], check=False)
    return result.returncode, result.stdout, result.stderr


def _prof(c: ContainerHandle, verb: str, *extra: str) -> tuple[int, str, str]:
    return _setforge(
        c, [verb, f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}", *extra]
    )


def _container_with_stub(
    docker_container: Callable[..., ContainerHandle],
) -> ContainerHandle:
    """Fresh container wired to the deterministic fake ``claude`` binary."""
    c = docker_container(env={"SETFORGE_CLAUDE_BIN": _FAKE_CLAUDE})
    c.write_text(_FAKE_CLAUDE, _FAKE_CLAUDE_SCRIPT)
    c.exec(["chmod", "+x", _FAKE_CLAUDE], check=True)
    assert _prof(c, "install")[0] == 0
    c.write_text(_LIVE, _LIVE_BODY)
    return c


def _draft_through_review(
    pyte_pty_session: Callable[..., PyteSession], c: ContainerHandle
) -> PyteSession:
    """Drive ``stage`` to the draft review screen: Share → Draft → (default
    instruction) → the stubbed rewrite. Leaves focus on the review bar."""
    s = pyte_pty_session(
        container=c.cid,
        cmd=[
            "uv",
            "run",
            "setforge",
            "stage",
            "stage_notes",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
        timeout=120.0,
    )
    s.expect_in_display(_FRAME_GLYPH, timeout=60.0)
    s.expect_in_display("Host paths", timeout=30.0)  # the lone workdir hunk
    s.send_keys("\r")  # Share (focused) → share sub-menu
    s.expect_in_display("Verbatim", timeout=30.0)  # sub-menu rendered
    s.send_keys("\r")  # Draft (Claude) (focused) → instruction prompt
    s.expect_in_display("guidance", timeout=30.0)  # the instruction hint
    s.send_keys("\r")  # empty instruction → let the (stubbed) model draft
    s.expect_in_display("SHAREABLE_DRAFT", timeout=60.0)  # the rewrite, in review
    return s


@pytest.mark.xdist_group("docker_daemon")
def test_share_draft_keep_local_shares_draft_keeps_host_live(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Keep-mine-local: tracked gets the shareable draft, live keeps the host
    bytes; compare stays clean and revert restores."""
    c = _container_with_stub(docker_container)

    s = _draft_through_review(pyte_pty_session, c)
    s.send_keys("\r")  # Keep mine local (default focus) → SHARED_DRAFTED, no adopt
    s.expect_in_display("1 drafted", timeout=30.0)
    s.wait_for_exit(timeout=60, expected_code=0)

    assert _prof(c, "sync", "--auto=use-live", "--yes")[0] == 0
    tracked = c.read_text(_TRACKED)
    assert _DRAFT_MARKER in tracked  # the draft promoted upstream
    assert "/home/tester" not in tracked  # host bytes NEVER leaked into tracked
    assert "/home/tester" in c.read_text(_LIVE)  # live keeps the host path

    # A blessed SHARED_DRAFTED divergence is NOT re-flagged as drift.
    assert _prof(c, "compare", "--check")[0] == 0

    # revert restores tracked to its pre-sync (base) state.
    assert _prof(c, "revert", "-y")[0] == 0
    assert _DRAFT_MARKER not in c.read_text(_TRACKED)


@pytest.mark.xdist_group("docker_daemon")
def test_share_draft_adopt_rewrites_live_and_tracked(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Adopt-locally: the live region is rewritten to the draft too, so live ==
    tracked == draft (no divergence, no host bytes anywhere)."""
    c = _container_with_stub(docker_container)

    s = _draft_through_review(pyte_pty_session, c)
    s.send_keys("\x1b[C\r")  # focus 0→1 = Adopt locally → SHARED_DRAFTED + adopt
    s.expect_in_display("1 drafted", timeout=30.0)
    s.wait_for_exit(timeout=60, expected_code=0)

    # Adopt rewrites live immediately (batched at persist), before any sync.
    live = c.read_text(_LIVE)
    assert _DRAFT_MARKER in live  # live adopted the draft
    assert "/home/tester" not in live  # the host original is gone

    assert _prof(c, "sync", "--auto=use-live", "--yes")[0] == 0
    tracked = c.read_text(_TRACKED)
    assert _DRAFT_MARKER in tracked
    assert "/home/tester" not in tracked
