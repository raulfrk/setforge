"""Docker E2E: interactive ``setforge init`` GIT/PATH source entry.

Before the audit fix, selecting GIT or PATH in the init source-config
picker silently collapsed back to SKIP. The fix collects the URL /
directory via a follow-up ``text_prompt`` and writes the matching
``source:`` block to ``local.yaml``. This drives the full-screen
themed ``button_bar`` / ``text_prompt`` widgets through the pyte PTY
harness and asserts the ``source: kind: path`` block lands in the
written ``local.yaml``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from ruamel.yaml import YAML

from tests.docker.conftest import ContainerHandle
from tests.docker.pyte_session import PyteSession

pytestmark = pytest.mark.e2e_docker

_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_CHOSEN_PATH = "/tmp/my-config-repo"


def test_init_path_source_writes_source_block(
    docker_container: Callable[..., ContainerHandle],
    pyte_pty_session: Callable[..., PyteSession],
) -> None:
    """Selecting PATH + typing a directory writes a path source block.

    Drives the themed ``button_bar`` / ``text_prompt`` widgets via the
    pyte harness (button_bar convention: ←/→ moves focus, Enter selects
    the focused button; text_prompt: type then Enter submits the buffer).
    Sequence: source button_bar → focus PATH → text_prompt → type the
    directory → apply-confirm button_bar → proceed.
    """
    c = docker_container()
    session = pyte_pty_session(
        container=c.cid,
        cmd=["uv", "run", "setforge", "init"],
        timeout=60.0,
    )

    # 1) Source-config button_bar. skip is button 0 (focused, initial=0),
    #    git URL is button 1, local path is button 2. Two right-arrows move
    #    focus onto "local path"; Enter selects it and exits the widget.
    session.expect_in_display("configure your config-repo source?", timeout=30.0)
    session.expect_in_display("local path", timeout=10.0)
    session.send_keys("\x1b[C")  # skip -> git URL
    session.send_keys("\x1b[C")  # git URL -> local path
    session.expect_in_display("«local path»", timeout=10.0)
    session.send_keys("\r")  # select the focused (local path) button

    # 2) text_prompt for the directory. Type the path, Enter submits the
    #    buffer directly (no OK button / Tab step, unlike the old input_dialog).
    session.expect_in_display("local config-repo source", timeout=15.0)
    session.send_keys(_CHOSEN_PATH)
    session.send_keys("\r")

    # 3) apply-confirm button_bar. proceed is button 0 (focused, initial=0);
    #    a bare Enter selects the focused (proceed) button.
    session.expect_in_display("ready to apply?", timeout=15.0)
    session.send_keys("\r")

    session.wait_for_exit(timeout=60.0, expected_code=0)

    # The written local.yaml carries the path source block built from the
    # interactively-entered directory (pre-fix it would have stayed SKIP).
    # The path is emitted JSON-quoted (YAML-injection hardening), so assert
    # the quoted literal AND that the document round-trips to the chosen path.
    local_yaml = c.read_text(_LOCAL_YAML)
    assert "kind: path" in local_yaml, local_yaml
    assert f'path: "{_CHOSEN_PATH}"' in local_yaml, local_yaml
    parsed = YAML(typ="safe").load(local_yaml)
    assert parsed["source"]["kind"] == "path", local_yaml
    assert parsed["source"]["path"] == _CHOSEN_PATH, local_yaml
