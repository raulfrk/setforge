"""Docker E2E: MCP-server registration + cargo-binary install.

Three behavior-preservation cases against a fresh Debian 12 container
with a REAL ``claude`` binary. The image now ships a real rust toolchain
(``~/.cargo/bin`` on PATH), so the missing-cargo case (b) explicitly
scrubs ``.cargo/bin`` from PATH to reproduce a cargo-less host:

(a) MCP register → assert present → revert → assert gone → idempotent
    reinstall. Drives the real ``claude mcp`` surface.
(b) cargo missing-toolchain: with cargo removed from PATH, ``install``
    emits the missing-cargo warning to stderr and STILL exits 0 (deploy
    happens).
(c) cargo skip-if-present: a dummy crate pre-registered with ``cargo``
    is NOT re-installed. This case stubs a fake ``cargo`` earlier on PATH
    whose ``install --list`` reports the crate so the skip path is
    exercised deterministically (independent of the real toolchain).

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
schema_version: '6.0'
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


def _native_registration(c: ContainerHandle, name: str):
    import json

    home = json.loads(c.read_text("/home/tester/.claude.json"))
    candidates = [
        ("user", home),
        ("local", home.get("projects", {}).get("/workspace", {})),
    ]
    project = c.exec(["cat", "/workspace/.mcp.json"], check=False)
    if project.returncode == 0:
        candidates.append(("project", json.loads(project.stdout)))
    found = [
        ([row["command"], *row.get("args", [])], scope)
        for scope, source in candidates
        if (row := source.get("mcpServers", {}).get(name)) is not None
    ]
    assert len(found) <= 1, found
    return found[0] if found else None


@pytest.mark.parametrize(
    ("prior_scope", "scope"),
    [("user", "project"), ("project", "local"), ("local", "user")],
)
def test_native_mcp_update_revert_redo_and_idempotence(
    docker_container: Callable[..., ContainerHandle], prior_scope: str, scope: str
) -> None:
    c = docker_container()
    _write_source(c, body=_MCP_YAML.replace("scope: user", f"scope: {scope}"))
    prior = ["echo", "old argument", 'quote"', "slash\\", "--old"]
    c.exec(["claude", "mcp", "add", "--scope", prior_scope, "echo-srv", "--", *prior])
    c.exec(
        [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            "manual-control",
            "--",
            "echo",
            "keep",
        ]
    )
    _install(c)
    assert _native_registration(c, "echo-srv") == (["echo", "hello"], scope)
    for expected in [(prior, prior_scope), (["echo", "hello"], scope)]:
        r = c.exec(
            ["uv", "run", "setforge", "revert", f"--profile={_PROFILE}", "--yes"],
            check=False,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert _native_registration(c, "echo-srv") == expected
        assert _native_registration(c, "manual-control") == (["echo", "keep"], "user")
    _install(c)
    assert _native_registration(c, "echo-srv") == (["echo", "hello"], scope)
    assert _native_registration(c, "manual-control") == (["echo", "keep"], "user")


def test_native_mcp_partial_replacement_failure_remains_revertible(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    import json

    c = docker_container()
    _write_source(
        c,
        body=_MCP_YAML.replace(
            "command: [echo, hello]", "command: [echo, FAIL_REPLACEMENT]"
        ),
    )
    c.exec(["claude", "mcp", "add", "--scope", "user", "echo-srv", "--", "echo", "old"])
    binary = c.exec(["sh", "-c", "command -v claude"]).stdout.strip()
    c.write_text(
        "/tmp/controlled-claude",
        f"""#!/bin/sh
for arg in "$@"; do
    if [ "$arg" = "FAIL_REPLACEMENT" ]; then
        echo "controlled replacement failure" >&2
        exit 1
    fi
done
exec {binary} "$@"
""",
    )
    c.exec(["chmod", "+x", "/tmp/controlled-claude"])
    result = c.exec(
        ["uv", "run", "setforge", "install", f"--profile={_PROFILE}", "--yes"],
        check=False,
        env={"SETFORGE_CLAUDE_BIN": "/tmp/controlled-claude"},
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert _native_registration(c, "echo-srv") is None
    files = c.exec(
        ["find", "/home/tester/.local/state/setforge", "-name", "mcp.json"]
    ).stdout.splitlines()
    assert len(files) == 1
    delta = json.loads(c.read_text(files[0]))
    assert delta["added"] == []
    assert delta["updated"] == [["echo-srv", ["echo", "old"], "user"]]
    result = c.exec(
        ["uv", "run", "setforge", "revert", f"--profile={_PROFILE}", "--yes"],
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _native_registration(c, "echo-srv") == (["echo", "old"], "user")


def test_native_mcp_revert_preserves_stale_registration(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    body = _MCP_YAML.replace(
        "mcp_servers:\n  echo-srv:",
        "mcp_servers:\n  early:\n    command: [echo, new]\n  echo-srv:",
    ).replace("      - echo-srv", "      - early\n      - echo-srv")
    _write_source(c, body=body)
    c.exec(["claude", "mcp", "add", "--scope", "user", "early", "--", "echo", "old"])
    _install(c)
    c.exec(["claude", "mcp", "remove", "--scope", "user", "echo-srv"])
    c.exec(
        [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            "echo-srv",
            "--",
            "echo",
            "external",
        ]
    )
    snapshot = """from pathlib import Path
import json
root = Path('/home/tester/.local/state/setforge')
print(json.dumps({str(p.relative_to(root)):p.read_bytes().hex()
    for p in root.rglob('*') if p.is_file()
    and not {'transitions', 'operations'} & set(p.relative_to(root).parts)},
    sort_keys=True))
"""
    before = c.exec(["uv", "run", "python", "-c", snapshot]).stdout
    result = c.exec(
        ["uv", "run", "setforge", "revert", f"--profile={_PROFILE}", "--yes"],
        check=False,
    )
    assert result.returncode != 0
    assert _native_registration(c, "echo-srv") == (["echo", "external"], "user")
    assert _native_registration(c, "early") == (["echo", "new"], "user")
    assert c.read_text("/tmp/out/foo.md") == "# foo\n"
    assert c.exec(["uv", "run", "python", "-c", snapshot]).stdout == before
    records = c.exec(
        ["find", "/home/tester/.local/state/setforge/transitions", "-name", "meta.json"]
    ).stdout.splitlines()
    assert len(records) == 1


# ---------------------------------------------------------------------------
# (b) cargo missing toolchain → warn + exit 0
# ---------------------------------------------------------------------------

_CARGO_YAML = """\
version: 1
schema_version: '6.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
packages:
  ast-grep:
    type: cargo
    crate: ast-grep
profiles:
  base:
    tracked_files:
      - foo
    packages:
      - ast-grep
"""


def test_cargo_missing_toolchain_warns_and_exits_zero(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_source(c, body=_CARGO_YAML)

    # The image now ships a real cargo (~/.cargo/bin). Scrub it from PATH
    # so this case reproduces a cargo-less host → install warns and
    # continues to exit 0. cargo is resolved via `shutil.which`, so a PATH
    # without .cargo/bin makes it invisible to the provisioner.
    res = c.exec(
        ["uv", "run", "setforge", "install", f"--profile={_PROFILE}", "--yes"],
        check=False,
        env={"PATH": "/home/tester/.local/bin:/usr/local/bin:/usr/bin:/bin"},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "cargo not found on PATH" in res.stderr, res.stderr
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
    c.exec(["git", "-C", _SRC_REPO, "init", "-b", "main"], check=True)

    # Place a fake cargo on PATH that reports ast-grep already installed.
    c.write_text("/home/tester/.local/bin/cargo", _FAKE_CARGO)
    c.exec(["chmod", "+x", "/home/tester/.local/bin/cargo"], check=True)

    res = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            "--yes",
            "--no-git-check",
        ],
        check=False,
        env={"PATH": "/home/tester/.local/bin:/usr/local/bin:/usr/bin:/bin"},
    )
    assert res.returncode == 0, res.stdout + res.stderr
    combined = res.stdout + res.stderr
    # The skip path ran: an already-present crate is filtered at plan time, so
    # no real `cargo install ast-grep` runs and nothing is (re)provisioned.
    assert "FAKE_CARGO_INSTALL_INVOKED" not in combined, combined
    assert "provisioned ast-grep" not in combined, combined
    assert "adopted package ownership: ast-grep" in combined, combined

    # The first pass changed metadata only. The durable claim makes the next
    # non-interactive run a normal managed no-op without another confirmation.
    repeated = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            f"--profile={_PROFILE}",
            "--no-git-check",
        ],
        check=False,
        env={"PATH": "/home/tester/.local/bin:/usr/local/bin:/usr/bin:/bin"},
    )
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    repeated_combined = repeated.stdout + repeated.stderr
    assert "FAKE_CARGO_INSTALL_INVOKED" not in repeated_combined, repeated_combined
    assert "adopted package ownership" not in repeated_combined, repeated_combined
