"""Docker E2E tests for the fresh-host welcome gate (setforge-7jg4).

Two cases:

- :func:`test_no_tty_raises` — fresh container, ``setforge install``
  invoked via plain ``docker exec`` (no TTY) without ``--yes``. The
  install MUST raise :class:`WelcomeRequiresInteractive` (rendered as
  ``error: ... requires --yes ...``) and exit non-zero WITHIN the
  per-call docker timeout. A hang would surface as a
  :class:`subprocess.TimeoutExpired` because the dialog blocks for
  user input that will never arrive.
- :func:`test_yes_skips_welcome_in_docker` — fresh container, install
  with ``--yes``. Skips the welcome entirely and runs the real
  pipeline through to completion.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import CONFIG_FIXTURE, ContainerHandle

pytestmark = pytest.mark.e2e_docker


_PROFILE: str = "test-minimal"


def test_no_tty_raises(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``setforge install`` without TTY + without --yes raises, doesn't hang.

    A fresh container has no ``~/.local/state/setforge/transitions/``,
    so :func:`setforge.cli._welcome.is_fresh_host` returns True. The
    install command is invoked via plain ``docker exec`` (no ``-t``),
    so ``sys.stdin.isatty()`` is False. Without ``--yes`` the welcome
    gate raises :class:`WelcomeRequiresInteractive`; the global handler
    in :func:`setforge.cli.main` renders it as
    ``error: ... requires --yes ...`` and exits 1.

    The load-bearing assertion is that the call completes (no hang)
    inside the per-call docker timeout. A hang would surface as
    :class:`subprocess.TimeoutExpired` raised by ``ContainerHandle.exec``.
    """
    c = docker_container()
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--no-git-check",
        ],
        check=False,
    )
    assert result.returncode != 0, (
        f"install exited 0 on a fresh host without --yes (expected the "
        f"welcome gate to raise):\n{result.stdout}\n{result.stderr}"
    )
    combined = (result.stderr or "") + (result.stdout or "")
    assert "requires --yes" in combined or "WelcomeRequiresInteractive" in combined, (
        f"non-TTY raise did not surface the welcome marker:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_yes_skips_welcome_in_docker(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``--yes`` on a fresh container skips the welcome and runs install.

    Anchors the spec's escape-hatch contract: a CI / scripted install
    on a brand-new host must work with ``--yes`` and without the user
    ever seeing the welcome panel.
    """
    c = docker_container()
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--no-git-check",
            "--yes",
        ],
        check=False,
    )
    assert result.returncode == 0, (
        f"install --yes failed on a fresh container:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The welcome panel header should NOT be present when --yes skips it.
    assert "fresh-host detected" not in result.stdout, (
        f"welcome panel rendered under --yes (expected to skip):\n"
        f"{result.stdout}"
    )


def test_auto_on_fresh_host_rejected(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """``--auto=use-tracked`` on a fresh container exits 2 with the
    'no drift exists on fresh host' message.

    Anchors anti-pattern check 6: ``--auto=*`` is meaningless on a
    fresh host (no drift exists yet) and must be rejected even when
    ``--yes`` is passed alongside.
    """
    c = docker_container()
    result = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--no-git-check",
            "--auto=use-tracked",
        ],
        check=False,
    )
    assert result.returncode == 2, (
        f"--auto=* on fresh host did not exit 2:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = (result.stderr or "") + (result.stdout or "")
    assert "no drift exists on fresh host" in combined


def test_welcome_panel_rendered_on_tty(
    docker_container: Callable[..., ContainerHandle],
    docker_pty_session: Callable[..., object],
) -> None:
    """On a real TTY, the welcome panel renders before the radiolist dialog.

    Drives ``setforge install`` via ``docker exec -it`` + pexpect so
    stdin is a TTY. Expects the welcome header to appear on the
    captured terminal stream, then sends Ctrl-C to abort (default safe
    choice). The assertion is on the header substring only — the
    radiolist dialog's rendering varies by terminal emulator and we
    don't want to assert against ANSI escape minutiae.
    """
    import pexpect  # type: ignore[import-untyped]

    c = docker_container()
    pty = docker_pty_session(
        c,
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            f"--config={CONFIG_FIXTURE}",
            "--no-git-check",
        ],
        timeout=60,
    )
    try:
        pty.expect("fresh-host detected")
        # Send Ctrl-C; the SIGINT handler restores the terminal and
        # exits the process. The handler is wrapped in try/finally so
        # the terminal state isn't left in raw mode.
        pty.sendcontrol("c")
        pty.expect(pexpect.EOF)
    finally:
        if pty.isalive():
            pty.close(force=True)
