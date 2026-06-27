"""A minimal resumable ``claude -p`` print-mode session — the shared primitive.

One :class:`ClaudeSession` is one conversation keyed by a preset ``uuid4``: turn
1 opens it with ``--session-id <id>``; every later turn resumes it with
``--resume <id>``. The session owns binary resolution, argv construction, the
sandbox flags, and the subprocess-fault → :class:`ClaudeSessionError` mapping;
it carries **no UI and no prompt content** so other callers (A4 claude-merge,
A5 hunk-staging auto-draft) reuse it verbatim.

Hardening (decision #3): the prompt is delivered on **stdin**, never argv (a
prompt leading with ``-`` would parse as a flag, and a long one would hit
``ARG_MAX``); all tools are disabled (``--tools ""``) so the model is a pure
text transform that cannot act on injected instructions; the child runs in its
own process group with a scrubbed environment and a neutral cwd so nothing
ambient (the config repo, ``$HOME`` secrets) leaks in.

Failure policy (decision #4): **every** runtime fault — non-zero exit, timeout,
empty or undecodable output, an unresolved or invalid ``claude`` binary — is
raised as a single re-promptable :class:`ClaudeSessionError`. Nothing here is
fatal; the caller maps it to a re-prompt.
"""

from __future__ import annotations

import functools
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from setforge.binaries import resolve_binary
from setforge.errors import SetforgeError

#: The text a session turn returns (the model's print-mode stdout).
type ClaudeText = str

_CLAUDE_BIN_NAME = "claude"
_TIMEOUT_S = 120
#: Per-region cost ceiling passed as ``--max-budget-usd``. A string because the
#: CLI flag takes a decimal literal; kept conservative (one region ≈ a few cents).
_MAX_BUDGET_USD = "0.50"
#: Cap a surfaced stderr tail so a runaway error body stays a one-line note.
_STDERR_TAIL_CHARS = 200


class ClaudeSessionError(SetforgeError):
    """A re-promptable ``claude -p`` runtime fault.

    Raised for a non-zero exit, a timeout, empty or non-UTF-8 output, OR an
    unresolved / invalid ``claude`` binary. Per the A4 failure policy ALL of
    these are re-promptable — :mod:`setforge.reconcile.claude_merge` maps this
    to ``CANCEL`` and nothing here aborts the file. The message carries a
    one-line tail of the child's stderr where one is available.
    """


@functools.lru_cache(maxsize=1)
def _resolve_claude_bin() -> Path:
    """Resolve the ``claude`` binary, mapping every miss to :class:`ClaudeSessionError`.

    Unlike :func:`setforge.claude_plugins._get_claude_bin` (which propagates a
    config fault so an install aborts loudly), A4 deliberately degrades: a
    missing binary OR an invalid override (``BinaryOverrideInvalid`` from
    :func:`resolve_binary`) both become a re-promptable
    :class:`ClaudeSessionError` (decision #4). Cached for the process; tests
    that change the resolved path between cases call ``cache_clear()``.
    """
    try:
        path = resolve_binary(_CLAUDE_BIN_NAME)
    except SetforgeError as exc:  # BinaryOverrideInvalid — degrade, don't propagate.
        raise ClaudeSessionError(f"claude binary unavailable: {exc}") from exc
    if path is None:
        raise ClaudeSessionError(
            "claude binary not found; install the Claude CLI or set "
            "--claude-bin / SETFORGE_CLAUDE_BIN / local.yaml"
        )
    return path


def _scrubbed_env() -> dict[str, str]:
    """A copy of the environment with setforge's own variables removed.

    Drops every ``SETFORGE_*`` key so the child never sees the config-repo
    source path or other engine state; keeps the rest (``PATH``, ``HOME`` for
    the model's own auth, etc.) so ``claude`` still runs.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("SETFORGE_")}


def _neutral_cwd() -> Path:
    """A neutral working directory for the child — the system temp dir.

    Never the config repo or ``$HOME``: with all tools disabled the model
    cannot read the filesystem anyway, but a neutral cwd is defence-in-depth so
    no project-level ``CLAUDE.md`` / ``.claude`` context is ambiently picked up.
    """
    return Path(tempfile.gettempdir())


def _tail(stderr: str) -> str:
    """The last non-empty line of ``stderr``, truncated for an inline note."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1].strip()
    if len(last) > _STDERR_TAIL_CHARS:
        last = last[:_STDERR_TAIL_CHARS] + "…"
    return last


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the child's whole process group, then wait on the direct child.

    The child runs with ``start_new_session=True`` so it leads its own group;
    SIGKILLing the group (not just the child) takes down any ``claude`` helper
    grandchildren too, so none is left orphaned on a timeout. ``wait()`` reaps
    the direct child here; re-parented grandchildren are reaped by init.
    Best-effort: a race where the group is already gone is benign.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    finally:
        proc.wait()


class ClaudeSession:
    """One resumable print-mode session, keyed by a preset ``uuid4``.

    The id is fixed at construction and never reused across regions. The first
    :meth:`send` opens the session (``--session-id``); subsequent calls resume
    it (``--resume``). A failed first turn leaves the session unopened, so the
    next :meth:`send` retries fresh rather than resuming a session that was
    never created.
    """

    __slots__ = ("_id", "_started")

    def __init__(self) -> None:
        self._id = uuid4().hex
        self._started = False

    def send(self, prompt: str) -> ClaudeText:
        """Run one turn; return its stdout, or raise :class:`ClaudeSessionError`.

        ``prompt`` is fed on **stdin**. Turn 1 carries ``--session-id <id>``;
        once a turn has succeeded, every later turn carries ``--resume <id>``.
        The session is only marked started after a turn succeeds.
        """
        claude = _resolve_claude_bin()
        session_flag = (
            ["--resume", self._id] if self._started else ["--session-id", self._id]
        )
        argv = [
            str(claude),
            "-p",
            *session_flag,
            "--output-format",
            "text",
            "--tools",
            "",  # disable ALL tools — pure text transform, immune to tool-use injection
            "--max-budget-usd",
            _MAX_BUDGET_USD,
        ]
        out = self._run(argv, prompt)
        self._started = True
        return out

    @staticmethod
    def _run(argv: list[str], prompt: str) -> ClaudeText:
        """Spawn the child, feed ``prompt`` on stdin, and validate its output."""
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",  # explicit UTF-8, never the locale codec
                start_new_session=True,  # own process group → killable on timeout
                env=_scrubbed_env(),
                cwd=str(_neutral_cwd()),
            )
        except OSError as exc:
            raise ClaudeSessionError(f"could not launch claude: {exc}") from exc
        try:
            out, err = proc.communicate(input=prompt, timeout=_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            _kill_group(proc)
            raise ClaudeSessionError(f"claude timed out after {_TIMEOUT_S}s") from exc
        except UnicodeDecodeError as exc:
            _kill_group(proc)
            raise ClaudeSessionError(
                "claude produced undecodable (non-UTF-8) output"
            ) from exc
        except KeyboardInterrupt:
            # The child leads its own session (start_new_session=True), so a
            # terminal Ctrl-C reaches only this parent — kill the group so no
            # claude process is orphaned, then let the interrupt propagate.
            _kill_group(proc)
            raise
        if proc.returncode != 0:
            tail = _tail(err)
            detail = f": {tail}" if tail else ""
            raise ClaudeSessionError(f"claude exited {proc.returncode}{detail}")
        if not out.strip():
            raise ClaudeSessionError("claude returned empty output")
        return out
