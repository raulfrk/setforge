"""Self-tests for :mod:`tests.docker.pyte_session`.

These are NOT marked ``e2e_docker`` — they exercise the byte-stream
to pyte-screen plumbing in isolation (no docker daemon, no real PTY).
The integration cases that drive a real ``docker exec -it`` live in
:mod:`tests.docker.test_e2e_docker_auto_confirm` under the
``e2e_docker`` marker.
"""

from __future__ import annotations

import pyte

from tests.docker.pyte_session import PyteSession


def test_pyte_byte_stream_clears_display_on_escape() -> None:
    """Feeding ``\\x1b[2J`` (clear-screen) blanks the pyte display.

    Sanity check that the harness's byte-stream → screen wiring obeys
    standard ANSI: write some text, then clear, then assert all 40
    lines are blank (each is a ``cols``-wide whitespace string).
    """
    screen = pyte.HistoryScreen(120, 40)
    stream = pyte.ByteStream(screen)

    stream.feed(b"some pre-clear content")
    assert "some pre-clear content" in "\n".join(screen.display)

    stream.feed(b"\x1b[2J\x1b[H")  # clear screen + cursor home
    assert all(line.strip() == "" for line in screen.display)


class _StubProcess:
    """Minimal stand-in for :class:`pexpect.spawn` used by the unit tests.

    Records how many times :meth:`close` was called so the idempotence
    assertion has something concrete to check; ``isalive`` always
    returns False so the harness never tries to pump bytes through us.
    """

    def __init__(self) -> None:
        self.close_calls: int = 0

    def close(self, *, force: bool) -> None:
        assert force is True, "harness must request force=True"
        self.close_calls += 1

    def isalive(self) -> bool:
        return False


def test_pyte_session_dataclass_holds_screen_stream_process() -> None:
    """PyteSession stores screen + stream + process as plain attributes."""
    screen = pyte.HistoryScreen(120, 40)
    stream = pyte.ByteStream(screen)
    process = _StubProcess()
    session = PyteSession(screen=screen, stream=stream, process=process)  # type: ignore[arg-type]
    assert session.screen is screen
    assert session.stream is stream
    assert len(session.display) == 40
    # No bytes fed yet → every line is the initial blank (cols-wide
    # whitespace string); pyte renders an unwritten screen as blanks.
    assert all(line.strip() == "" for line in session.display)


def test_pyte_session_close_is_idempotent() -> None:
    """:meth:`PyteSession.close` only calls the underlying ``close`` once."""
    screen = pyte.HistoryScreen(120, 40)
    stream = pyte.ByteStream(screen)
    process = _StubProcess()
    session = PyteSession(screen=screen, stream=stream, process=process)  # type: ignore[arg-type]
    session.close()
    session.close()
    assert process.close_calls == 1


def test_pyte_session_display_reflects_fed_bytes() -> None:
    """:attr:`PyteSession.display` mirrors :attr:`screen.display`."""
    screen = pyte.HistoryScreen(120, 40)
    stream = pyte.ByteStream(screen)
    session = PyteSession(screen=screen, stream=stream, process=_StubProcess())  # type: ignore[arg-type]
    stream.feed(b"hello pyte world")
    assert "hello pyte world" in "\n".join(session.display)
