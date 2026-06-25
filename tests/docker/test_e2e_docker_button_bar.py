"""Narrow-terminal Docker e2e for :func:`setforge.ui.widgets.button_bar`.

Unlike the unit suite (which drives the Application headlessly via
``create_app_session`` + a pipe), this exercises the widget under a REAL TTY at
a narrow ``cols=46`` so the responsive frame + button-row wrapping are proven
against a genuine terminal. The driver is :mod:`tests.docker._button_bar_demo`,
which runs the bar once and prints ``CHOSE:<value>`` / ``CHOSE:CANCEL``.

Harness: the pyte-emulated PTY (:class:`tests.docker.pyte_session.PyteSession`)
— a full-screen prompt_toolkit Application splashes its frame across
cursor-positioned bytes, so we anchor on the EMULATED SCREEN (``.display``),
never the raw byte stream. The rendered framed glyph (``┌``) is the focus fence
we wait on before sending keys.

prompt_toolkit pitfalls baked in:

* Esc-cancel is sent as a DOUBLE-TAP ``\\x1b\\x1b`` — a lone ``\\x1b`` is the
  Alt/arrow prefix and the key processor waits for a follow-up byte, so a
  single Esc fires late/never under a PTY. Two in a row resolve immediately.
* Arrow-right is ``\\x1b[C``; Enter is ``\\r`` (carriage return, not ``\\n``).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

# The demo driver, invoked inside the container workspace under a real TTY.
_DEMO_CMD = ["uv", "run", "python", "tests/docker/_button_bar_demo.py"]

# A box-drawing glyph from the frame's top rule: the render fence we wait on
# before sending input (the title text could land split across cursor moves,
# but the corner glyph paints as one cell).
_FRAME_GLYPH = "┌"

# Narrow terminal: forces the responsive frame width down and the four buttons
# to wrap to more than one row.
_COLS = 46
_LINES = 24


@pytest.mark.xdist_group("docker_daemon")
def test_button_bar_arrow_select(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Right-arrow then Enter selects the second button (``theirs``)."""
    c = docker_container()
    session = pyte_pty_session(
        container=c.cid,
        cmd=_DEMO_CMD,
        cols=_COLS,
        lines=_LINES,
        timeout=120.0,
    )
    # Wait for the frame to render — the focus fence.
    session.expect_in_display(_FRAME_GLYPH, timeout=60.0)
    # The button row must have wrapped: at cols=46 the four bracketed labels
    # cannot fit on one rule, so a wrapped row carries a focused/idle label.
    session.expect_in_display("Ours", timeout=30.0)
    session.send_keys("\x1b[C\r")  # arrow-right → "Theirs", then Enter
    session.expect_in_display("CHOSE:theirs", timeout=60.0)
    session.wait_for_exit(timeout=60, expected_code=0)


@pytest.mark.xdist_group("docker_daemon")
def test_button_bar_double_esc_cancels(
    pyte_pty_session: Callable[..., PyteSession],
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A double-tap Esc cancels the bar, printing ``CHOSE:CANCEL``."""
    c = docker_container()
    session = pyte_pty_session(
        container=c.cid,
        cmd=_DEMO_CMD,
        cols=_COLS,
        lines=_LINES,
        timeout=120.0,
    )
    session.expect_in_display(_FRAME_GLYPH, timeout=60.0)
    session.send_keys("\x1b\x1b")  # double-tap Esc → resolves immediately
    session.expect_in_display("CHOSE:CANCEL", timeout=60.0)
    session.wait_for_exit(timeout=60, expected_code=0)
