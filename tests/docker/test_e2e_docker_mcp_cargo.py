"""Docker E2E: MCP-server registration + cargo-binary install.

Three behavior-preservation cases against a fresh Debian 12 container
that has a REAL ``claude`` binary but NO rust/cargo toolchain (by design
— see the spec's e2e-coverage note):

(a) MCP register → assert present → revert → assert gone → idempotent
    reinstall. Drives the real ``claude mcp`` surface.
(b) cargo missing-toolchain: ``install`` emits the missing-cargo warning
    to stderr and STILL exits 0 (deploy happens). No rust in the image.
(c) cargo skip-if-present: a dummy crate pre-registered with ``cargo``
    is NOT re-installed. Because the image has no cargo, this case stubs a
    fake ``cargo`` on PATH whose ``install --list`` reports the crate so
    the skip path is exercised without a real toolchain.

The real ``cargo install`` subprocess is unit-tested with a mock
(``tests/test_cargo.py``); bloating the image with a rust toolchain for
one feature is deliberately out of scope.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_SRC_REPO = "/tmp/cfg-mcp-cargo"
_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_PROFILE = "base"


def _write_source(c: ContainerHandle, *, body: str) -> None:
    """Write a minimal config repo + point local.yaml's source at it."""
    c.write_text(f"{_SRC_REPO}/setforge.yaml", body)
    c.write_text(f"{_SRC_REPO}/tracked/foo.md", "# foo\n")
    c.write_text(
        _HOME_LOCAL_YAML,
        f"source:\n  kind: path\n  path: {_SRC_REPO}\n",
    )


def _install(c: ContainerHandle, *, check: bool = True):
    return c.exec(
        ["uv", "run", "setforge", "install", f"--profile={_PROFILE}", "--yes"],
        check=check,
    )


# ---------------------------------------------------------------------------
# (a) MCP register → revert → reinstall (idempotent)
# ---------------------------------------------------------------------------

_MCP_YAML = """\
version: 1
schema_version: '1.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
mcp_servers:
  echo-srv:
    command: [echo, hello]
    scope: user
profiles:
  base:
    tracked_files:
      - foo
    mcp_servers:
      - echo-srv
"""


def test_mcp_register_revert_reinstall(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_source(c, body=_MCP_YAML)

    # First install registers the server via the real `claude mcp add`.
    res = _install(c)
    assert res.returncode == 0, res.stdout + res.stderr

    listed = c.exec(["claude", "mcp", "list"], check=False)
    combined = listed.stdout + listed.stderr
    assert "echo-srv" in combined, combined

    # Revert removes it.
    rev = c.exec(
        ["uv", "run", "setforge", "revert", f"--profile={_PROFILE}", "--yes"],
        check=False,
    )
    assert rev.returncode == 0, rev.stdout + rev.stderr
    listed_after = c.exec(["claude", "mcp", "list"], check=False)
    assert "echo-srv" not in (listed_after.stdout + listed_after.stderr), (
        listed_after.stdout + listed_after.stderr
    )

    # Reinstall is idempotent: an already-registered server (or a fresh
    # re-add) must not surface a spurious failure or traceback.
    res2 = _install(c, check=False)
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "Traceback (most recent call last)" not in (res2.stdout + res2.stderr)
    listed2 = c.exec(["claude", "mcp", "list"], check=False)
    assert "echo-srv" in (listed2.stdout + listed2.stderr)


# ---------------------------------------------------------------------------
# (b) cargo missing toolchain → warn + exit 0
# ---------------------------------------------------------------------------

_CARGO_YAML = """\
version: 1
schema_version: '1.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
profiles:
  base:
    tracked_files:
      - foo
    cargo_binaries:
      - ast-grep
"""


def test_cargo_missing_toolchain_warns_and_exits_zero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_source(c, body=_CARGO_YAML)

    # Image has no cargo on PATH → install warns and continues to exit 0.
    res = _install(c, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "skipping cargo binaries" in res.stderr, res.stderr
    assert "ast-grep" in res.stderr, res.stderr
    # Deploy still happened.
    deployed = c.read_text("/tmp/out/foo.md")
    assert "# foo" in deployed


# ---------------------------------------------------------------------------
# (c) cargo skip-if-present (dummy cargo on PATH reports the crate)
# ---------------------------------------------------------------------------

_FAKE_CARGO = """\
#!/bin/sh
# Minimal fake `cargo` for the skip-if-present e2e case.
if [ "$1" = "install" ] && [ "$2" = "--list" ]; then
  printf 'ast-grep v0.1.0:\\n    sg\\n'
  exit 0
fi
if [ "$1" = "install" ]; then
  # If setforge reaches here, the skip-if-present check FAILED. Mark it.
  echo "FAKE_CARGO_INSTALL_INVOKED:$2" >&2
  exit 0
fi
exit 0
"""


def test_cargo_skip_if_present_does_not_invoke_install(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_source(c, body=_CARGO_YAML)

    # Place a fake cargo on PATH that reports ast-grep already installed.
    c.write_text("/home/tester/.local/bin/cargo", _FAKE_CARGO)
    c.exec(["chmod", "+x", "/home/tester/.local/bin/cargo"], check=True)

    res = c.exec(
        ["uv", "run", "setforge", "install", f"--profile={_PROFILE}", "--yes"],
        check=False,
        env={"PATH": "/home/tester/.local/bin:/usr/local/bin:/usr/bin:/bin"},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    combined = res.stdout + res.stderr
    # The skip path ran: no real `cargo install ast-grep` was invoked.
    assert "FAKE_CARGO_INSTALL_INVOKED" not in combined, combined
    assert "already installed (skip)" in combined, combined
