"""Docker E2E: failing `claude` subprocesses surface as clean errors.

Before the audit fix, several marketplace/plugin commands caught only
the tool-missing case, so a non-zero ``claude`` invocation escaped as a
raw Python traceback (``CalledProcessError`` / ``TimeoutExpired`` are not
``SetforgeError``, so the global entry-point handler does not catch
them). The fix catches both at each command boundary and exits 1 with a
clean ``error:`` line.

This exercises a network-free failure: ``marketplace update`` of a
marketplace that was never registered — ``claude`` rejects it from its
local registry without a fetch. The invariant asserted is the contract:
**no Python traceback reaches the user.**
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_SRC_REPO = "/tmp/cfg-plugin-err"
_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"


def _bootstrap(c: ContainerHandle) -> None:
    c.write_text(
        f"{_SRC_REPO}/setforge.yaml",
        "version: 1\n"
        "schema_version: '1.0'\n"
        "tracked_files:\n"
        "  foo:\n"
        "    src: foo.md\n"
        "    dst: /tmp/out/foo.md\n"
        "profiles:\n"
        "  base:\n"
        "    tracked_files:\n"
        "      - foo\n",
    )
    c.write_text(f"{_SRC_REPO}/tracked/foo.md", "# foo\n")
    c.write_text(
        _HOME_LOCAL_YAML,
        f"source:\n  kind: path\n  path: {_SRC_REPO}\n",
    )


def test_marketplace_update_unregistered_no_traceback(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A failing `claude plugin marketplace update` must not escape as a
    traceback — the command catches it and renders a clean error."""
    c = docker_container()
    _bootstrap(c)
    res = c.exec(
        ["uv", "run", "setforge", "marketplace", "update", "never-registered-xyz"],
        check=False,
    )
    combined = res.stdout + res.stderr
    # The ec2o.51 contract: subprocess errors never reach the user as a
    # raw Python traceback. (Whether claude exits 0 or non-zero, no
    # traceback should appear; without the fix a non-zero exit escaped.)
    assert "Traceback (most recent call last)" not in combined, combined
    assert "CalledProcessError" not in combined, combined
