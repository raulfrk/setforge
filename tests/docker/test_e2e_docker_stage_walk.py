"""Docker e2e for the A5 ``setforge stage`` walk (share / keep / skip / quit).

The interactive walk renders a full-screen prompt_toolkit ``button_bar`` per
hunk, so we drive it through the pyte-emulated PTY (:class:`PyteSession`) and
anchor on the EMULATED screen (``.display``), never the raw byte stream — see
:mod:`tests.docker.pyte_session`. The non-TTY exit-2 guard and the read-only
``stage --list`` JSON projection ship as plain ``docker exec`` (no PTY) cases.

Profile ``test-stage`` (``tests/fixtures/e2e/setforge.test.yaml``) declares one
PLAIN tracked_file ``stage_notes`` (``stage/notes.md``) so it rides the reconcile
staging path. The journey: ``install`` seeds the merge base, a live edit opens
two diff hunks (a shareable ``## Shell`` insertion + a host-specific
``## Host paths`` edit), the walk classifies them, ``sync`` promotes only the
SHARED hunk into tracked, and a demote re-stage reverts it.

Key conventions (see the pyte_session docstring): arrow-right is ``\\x1b[C``,
arrow-left ``\\x1b[D`` (focus wraps), Enter is ``\\r`` (NOT ``\\n``). Focus is
index-based and deterministic, so navigation never depends on the hidden
first-letter accelerators.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_PROFILE = "test-stage"
_TRACKED = "/workspace/tests/fixtures/e2e/tracked/stage/notes.md"
_LIVE = "/home/tester/.setforge_e2e/stage/notes.md"

# The fixture base (== tracked src on disk and the live bytes right after install).
_BASE_BODY = (
    "# Shareable notes\n\n"
    "## Tooling\n"
    "Use rg not grep; fd not find.\n\n"
    "## Host paths\n"
    "workdir: /home/generic\n"
)
# Live edit: insert a shareable ## Shell section + change the host-specific
# workdir. Two independent diff hunks, in base-position order (Shell first).
_LIVE_BODY = (
    "# Shareable notes\n\n"
    "## Tooling\n"
    "Use rg not grep; fd not find.\n\n"
    "## Shell\n"
    "Prefer zsh over bash.\n\n"
    "## Host paths\n"
    "workdir: /home/tester\n"
)

_FRAME_GLYPH = "┌"  # ┌ — the bar's top-rule corner; the render fence.


def _setforge(c: ContainerHandle, args: list[str]) -> tuple[int, str, str]:
    result = c.exec(["uv", "run", "setforge", *args], check=False)
    return result.returncode, result.stdout, result.stderr


def _install(c: ContainerHandle) -> tuple[int, str, str]:
    return _setforge(
        c, ["install", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"]
    )


def _sync(c: ContainerHandle) -> tuple[int, str, str]:
    return _setforge(
        c, ["sync", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}", "-y"]
    )


def _stage_session(
    pyte_pty_session: Callable[..., PyteSession], c: ContainerHandle
) -> PyteSession:
    """Spawn an interactive ``setforge stage stage_notes`` over the pyte PTY."""
    return pyte_pty_session(
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


@pytest.mark.xdist_group("docker_daemon")
def test_stage_walk_shares_one_hunk_then_demotes(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Share the ## Shell hunk + keep ## Host paths local → sync promotes only
    the shared hunk into tracked; a demote re-stage reverts it."""
    c = docker_container()
    assert _install(c)[0] == 0
    c.write_text(_LIVE, _LIVE_BODY)

    # --- walk 1: Share (verbatim) the Shell hunk, Keep-local the Host-paths hunk.
    s = _stage_session(pyte_pty_session, c)
    s.expect_in_display(_FRAME_GLYPH, timeout=60.0)
    s.expect_in_display("Shell", timeout=30.0)  # hunk 1 = the ## Shell insertion
    s.send_keys("\r")  # Share (focused) → opens the share sub-menu
    s.expect_in_display("Verbatim", timeout=30.0)
    s.send_keys("\x1b[C\r")  # → Verbatim → Decision(SHARED)
    s.expect_in_display("Host paths", timeout=30.0)  # hunk 2 = ## Host paths edit
    s.send_keys("\x1b[C\r")  # focus 0→1 = Keep local → Decision(LOCAL)
    s.expect_in_display("1 shared", timeout=30.0)  # summary: 1 shared  1 local
    s.wait_for_exit(timeout=60, expected_code=0)

    assert _sync(c)[0] == 0
    tracked = c.read_text(_TRACKED)
    assert "## Shell" in tracked  # shared hunk promoted verbatim
    assert "Prefer zsh over bash." in tracked
    assert "workdir: /home/generic" in tracked  # kept-local hunk held at base
    assert "/home/tester" not in tracked  # host bytes never leaked upstream
    assert "/home/tester" in c.read_text(_LIVE)  # live keeps the host edit

    # --- walk 2: demote the now-SHARED Shell hunk back to LOCAL.
    s2 = _stage_session(pyte_pty_session, c)
    s2.expect_in_display(_FRAME_GLYPH, timeout=60.0)
    s2.expect_in_display("Shell", timeout=30.0)
    s2.send_keys("\x1b[C\r")  # Shell SHARED (focus 0) → 0→1 = Keep local (demote)
    s2.expect_in_display("Host paths", timeout=30.0)
    s2.send_keys("\r")  # Host paths LOCAL (focus already on Keep local) → keep
    s2.expect_in_display("0 shared", timeout=30.0)  # summary: 0 shared  2 local
    s2.wait_for_exit(timeout=60, expected_code=0)

    assert _sync(c)[0] == 0
    # Both hunks now LOCAL → tracked is byte-restored to the base fixture.
    assert c.read_text(_TRACKED) == _BASE_BODY


@pytest.mark.xdist_group("docker_daemon")
def test_stage_walk_skip_then_quit_classifies_nothing(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Skip the first hunk then Quit the walk → no hunk is classified
    shared/local; both stay PENDING (the file remains fully unstaged).

    Asserted at the classification level (``--list``), NOT via sync: A5 staging
    is opt-in per file, so an unstaged file's sync keeps the legacy verbatim
    absorb — "nothing staged" means "nothing classified", not "tracked frozen".
    """
    c = docker_container()
    assert _install(c)[0] == 0
    c.write_text(_LIVE, _LIVE_BODY)

    s = _stage_session(pyte_pty_session, c)
    s.expect_in_display(_FRAME_GLYPH, timeout=60.0)
    s.expect_in_display("Shell", timeout=30.0)
    s.send_keys("\x1b[C\x1b[C\r")  # focus 0→2 = Skip (leave unchanged) → next hunk
    s.expect_in_display("Host paths", timeout=30.0)
    s.send_keys("\x1b[D\r")  # focus 0→left wraps to 3 = Quit → stop the walk
    s.wait_for_exit(timeout=60, expected_code=0)

    # Skip + Quit recorded no classification: both hunks are still PENDING.
    rc, out, err = _setforge(
        c,
        [
            "-o",
            "json",
            "stage",
            "--list",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, err
    (row,) = json.loads(out)["data"]
    assert (row["shared"], row["local"], row["pending"]) == (0, 0, 2), row


@pytest.mark.xdist_group("docker_daemon")
def test_stage_non_tty_exits_2(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``stage <file>`` on a non-TTY stdin refuses with exit 2 + a hint."""
    c = docker_container()
    assert _install(c)[0] == 0
    c.write_text(_LIVE, _LIVE_BODY)

    rc, _out, err = _setforge(
        c,
        ["stage", "stage_notes", f"--profile={_PROFILE}", f"--config={CONFIG_FIXTURE}"],
    )
    assert rc == 2, err
    assert "interactive" in err
    assert "stage --list" in err


@pytest.mark.xdist_group("docker_daemon")
def test_stage_list_json_projection(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``-o json stage --list`` emits the versioned envelope with per-file
    share/local/pending counts."""
    c = docker_container()
    assert _install(c)[0] == 0
    c.write_text(_LIVE, _LIVE_BODY)  # two unclassified (PENDING) hunks

    rc, out, err = _setforge(
        c,
        [
            "-o",
            "json",
            "stage",
            "--list",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
        ],
    )
    assert rc == 0, err
    envelope = json.loads(out)
    assert envelope["command"] == "stage"
    rows = {row["name"]: row for row in envelope["data"]}
    assert len(rows) == 1, envelope
    (row,) = rows.values()
    assert row["pending"] == 2  # both hunks unclassified
    assert row["shared"] == 0
    assert row["local"] == 0
