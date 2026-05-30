"""pyte-backed PTY session harness for full-screen TUI Docker e2e tests.

prompt_toolkit's ``radiolist_dialog`` / ``input_dialog`` / ``yes_no_dialog``
all run as full-screen ``Application`` instances on a TTY: every redraw
emits cursor-positioning ANSI (``\\x1b[H``, ``\\x1b[<row>;<col>H``,
``\\x1b[2J``, etc.) that pexpect's line-oriented matcher cannot reliably
anchor on. ``pexpect.expect(needle)`` blocks until ``needle`` shows up
in the raw byte stream, but a full-screen redraw can splash ``needle``
across non-contiguous bytes (the dialog clears the screen, paints a
border, then paints the title cell-by-cell with cursor moves between).

The harness layers on top of pexpect:

1. ``docker exec -it`` is spawned via :func:`pexpect.spawn` with no
   text encoding (raw bytes — pyte's :class:`pyte.ByteStream` consumes
   bytes, not strings).
2. Output bytes are pumped into a :class:`pyte.HistoryScreen` (120 cols
   by 40 lines by default; :class:`HistoryScreen` retains scrollback so
   ``.display`` keeps working after the dialog clears).
3. Tests anchor on the EMULATED SCREEN, not the raw byte stream:
   :meth:`PyteSession.expect_in_display` polls the screen until
   ``needle`` appears anywhere in the rendered display.
4. Key input is sent as raw bytes via :meth:`PyteSession.send_keys`;
   ANSI escape sequences (``\\x1b[A`` arrow up, ``\\r`` Enter, etc.)
   pass through unchanged.

Anti-smell items the docstring explicitly bakes in:

- ``docker exec -it`` (with ``-it``) is REQUIRED — without ``-t``,
  prompt_toolkit fast-paths to a non-TTY renderer that emits plain text
  and the dialog never appears in the screen buffer.
- :class:`pyte.HistoryScreen` (NOT :class:`pyte.Screen`) so scrollback
  survives the dialog's ``\\x1b[2J`` clear-screen.
- Arrow keys: ``\\x1b[A`` / ``\\x1b[B`` / ``\\x1b[C`` / ``\\x1b[D``
  (up / down / right / left). Enter: ``\\r`` (carriage return, NOT
  ``\\n``).
- :meth:`expect_in_display` polls in 0.1s chunks via
  :meth:`pexpect.spawn.read_nonblocking` until ``needle`` lands in the
  display or the per-call timeout expires.
- Pyte ``>=0.8.2`` minimum: earlier versions have an off-by-one in
  :class:`HistoryScreen` scrollback semantics.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field

import pexpect  # type: ignore[import-untyped]  # pexpect ships no py.typed
import pyte

_DEFAULT_COLS: int = 120
_DEFAULT_LINES: int = 40
_DEFAULT_SPAWN_TIMEOUT_S: float = 30.0
_DEFAULT_EXPECT_TIMEOUT_S: float = 5.0
_READ_CHUNK_BYTES: int = 4096
_READ_POLL_INTERVAL_S: float = 0.1


@dataclass(slots=True)
class PyteSession:
    """One ``docker exec -it`` PTY session emulated through pyte.

    Attributes
    ----------
    screen
        The pyte :class:`HistoryScreen` whose ``.display`` field exposes
        the rendered line buffer (each entry is a ``cols``-wide string,
        right-padded with spaces).
    stream
        The :class:`pyte.ByteStream` driving ``screen``.
    process
        The underlying :class:`pexpect.spawn` PTY (bytes mode).
    """

    screen: pyte.HistoryScreen
    stream: pyte.ByteStream
    process: pexpect.spawn
    _closed: bool = field(default=False, init=False)

    @classmethod
    def spawn(
        cls,
        *,
        container: str,
        cmd: list[str],
        cols: int = _DEFAULT_COLS,
        lines: int = _DEFAULT_LINES,
        timeout: float = _DEFAULT_SPAWN_TIMEOUT_S,
    ) -> PyteSession:
        """Spawn ``docker exec -it <container> <cmd>`` under a pyte-emulated PTY.

        ``cols`` x ``lines`` define the emulated screen size. The values
        also propagate via the ``-e COLUMNS`` / ``-e LINES`` env vars on
        ``docker exec`` so prompt_toolkit's renderer matches the pyte
        screen geometry (otherwise the dialog wraps differently than the
        emulator expects and ``.display`` cells look mangled).
        """
        screen = pyte.HistoryScreen(cols, lines)
        stream = pyte.ByteStream(screen)
        argv = [
            "exec",
            "-it",
            "-e",
            f"COLUMNS={cols}",
            "-e",
            f"LINES={lines}",
            "-e",
            "TERM=xterm-256color",
            container,
            *cmd,
        ]
        process = pexpect.spawn(
            "docker",
            argv,
            encoding=None,  # bytes mode — pyte.ByteStream consumes bytes
            timeout=timeout,
            dimensions=(lines, cols),
        )
        return cls(screen=screen, stream=stream, process=process)

    def send_keys(self, keys: str) -> None:
        """Send a key sequence to the PTY as UTF-8 bytes.

        ANSI escapes pass through as-is — arrow up is ``"\\x1b[A"``,
        arrow down is ``"\\x1b[B"``, Enter is ``"\\r"``. The argument
        is encoded once with ``encoding="utf-8"``.
        """
        self.process.send(keys.encode("utf-8"))

    @property
    def display(self) -> list[str]:
        """Return the current screen content as a list of ``lines`` strings.

        Each entry is ``cols`` characters wide, right-padded with
        spaces. Pump fresh bytes through :meth:`_pump_once` before
        reading if you need an up-to-date view; :meth:`expect_in_display`
        does this automatically.
        """
        return list(self.screen.display)

    def expect_in_display(
        self,
        needle: str,
        timeout: float = _DEFAULT_EXPECT_TIMEOUT_S,
    ) -> None:
        """Block until ``needle`` appears anywhere in the display.

        Polls :meth:`pexpect.spawn.read_nonblocking` in
        ``_READ_POLL_INTERVAL_S``-second slices, feeding each chunk into
        the pyte stream, then scans ``self.display`` joined by newlines.
        Raises :class:`TimeoutError` on miss.
        """
        deadline = time.monotonic() + timeout
        while True:
            self._pump_once()
            if needle in "\n".join(self.display):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"expect_in_display({needle!r}) timed out after {timeout:.1f}s; "
                    f"last display:\n" + "\n".join(self.display)
                )

    def wait_for_exit(
        self,
        *,
        timeout: float,
        expected_code: int,
    ) -> None:
        """Wait for the PTY process to exit; assert the exit code matches.

        Drains any remaining output into the pyte stream so a final
        :meth:`display` after :meth:`wait_for_exit` reflects the
        terminal state right before exit. Raises :class:`AssertionError`
        on exit-code mismatch and :class:`TimeoutError` on hang.
        """
        deadline = time.monotonic() + timeout
        while self.process.isalive():
            self._pump_once()
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"wait_for_exit timed out after {timeout:.1f}s; process still alive"
                )
        # Drain any final bytes the child wrote between the last poll
        # and exit.
        self._pump_once()
        actual = self.process.exitstatus
        if actual != expected_code:
            raise AssertionError(
                f"PTY exited with code {actual}, expected {expected_code}; "
                f"last display:\n" + "\n".join(self.display)
            )

    def close(self) -> None:
        """Force-kill the PTY child if still alive. Idempotent.

        Suppresses :class:`pexpect.ExceptionPexpect` / :class:`OSError`
        from the underlying ``close``: best-effort teardown — a process
        that already exited raises here but there's nothing left to
        clean up.
        """
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(pexpect.ExceptionPexpect, OSError):
            self.process.close(force=True)

    def _pump_once(self) -> None:
        """Read one non-blocking chunk from the PTY into the pyte stream.

        Empty reads and timeouts are normal (the child may be idle on
        the dialog); :class:`pexpect.EOF` means the child exited and we
        stop pumping. Other pexpect errors propagate.
        """
        try:
            chunk = self.process.read_nonblocking(
                size=_READ_CHUNK_BYTES,
                timeout=_READ_POLL_INTERVAL_S,
            )
        except pexpect.TIMEOUT:
            return
        except pexpect.EOF:
            return
        if chunk:
            self.stream.feed(chunk)
