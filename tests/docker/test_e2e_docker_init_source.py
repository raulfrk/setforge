"""Docker E2E: interactive ``setforge init`` GIT/PATH source entry.

Before the audit fix, selecting GIT or PATH in the init source-config
radiolist silently collapsed back to SKIP. The fix collects the URL /
directory via a follow-up ``input_dialog`` and writes the matching
``source:`` block to ``local.yaml``. This drives the full-screen
prompt_toolkit dialogs through the pyte PTY harness and asserts the
``source: kind: path`` block lands in the written ``local.yaml``.
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

    Drives prompt_toolkit dialogs via the pyte harness (per the
    radiolist convention: arrow to highlight, Enter commits the radio,
    Tab to OK, Enter submits). Sequence: source radiolist → pick PATH →
    input_dialog → type the directory → apply-confirm radiolist → proceed.
    """
    c = docker_container()
    session = pyte_pty_session(
        container=c.cid,
        cmd=["uv", "run", "setforge", "init"],
        timeout=60.0,
    )

    # 1) Source-config radiolist. Default is SKIP (first item); arrow
    #    down twice to PATH ("local path"), Enter to commit the radio.
    session.expect_in_display("configure your config-repo source?", timeout=30.0)
    session.expect_in_display("local path", timeout=10.0)
    session.send_keys("\x1b[B")  # SKIP -> git URL
    session.send_keys("\x1b[B")  # git URL -> local path
    session.send_keys("\r")  # commit the PATH radio
    session.expect_in_display("(*) local path", timeout=10.0)
    session.send_keys("\t")  # focus OK
    session.send_keys("\r")  # submit the radiolist

    # 2) input_dialog for the directory. Type the path, Tab to OK, Enter.
    session.expect_in_display("local config-repo source", timeout=15.0)
    session.send_keys(_CHOSEN_PATH)
    session.send_keys("\t")
    session.send_keys("\r")

    # 3) apply-confirm radiolist. Default PROCEED (first item); Tab to OK,
    #    Enter submits with the default selection.
    session.expect_in_display("ready to apply?", timeout=15.0)
    session.send_keys("\t")
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
