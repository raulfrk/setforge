"""Docker E2E for a mixed Claude/Codex lifecycle across CLI processes."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_ROOT = "/tmp/codex-parity"
_CONFIG = f"{_ROOT}/setforge.yaml"
_CODEX_HOME = "/home/tester/.codex-parity"
_CODEX_BIN = f"{_ROOT}/codex"
_CODEX_LOG = f"{_ROOT}/codex.log"
_ENV = {"CODEX_HOME": _CODEX_HOME, "SETFORGE_CODEX_BIN": _CODEX_BIN}

_FAKE_CODEX = """#!/usr/bin/env python3
import json
import pathlib
import sys

state_path = pathlib.Path("/tmp/codex-parity/codex-state.json")
state = (
    json.loads(state_path.read_text())
    if state_path.exists()
    else {"plugins": [], "marketplaces": []}
)
args = [arg for arg in sys.argv[1:] if arg != "--json"]
with pathlib.Path("/tmp/codex-parity/codex.log").open("a") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
if args == ["plugin", "list", "--available"]:
    payload = {"installed": state["plugins"]}
elif args == ["plugin", "marketplace", "list"]:
    payload = {"marketplaces": state["marketplaces"]}
elif args[:3] == ["plugin", "marketplace", "add"]:
    root = args[3]
    state["marketplaces"] = [{"name": pathlib.Path(root).name, "root": root}]
    payload = {"success": True}
elif args[:2] == ["plugin", "add"]:
    plugin_id = args[2]
    name, _, marketplace = plugin_id.partition("@")
    installed = {
        "pluginId": plugin_id,
        "name": name,
        "marketplaceName": marketplace,
    }
    state["plugins"] = [installed]
    payload = {
        **installed,
        "version": "1.0.0",
        "installedPath": f"/tmp/codex-parity/plugins/{name}",
        "authPolicy": None,
    }
elif args[:2] == ["plugin", "remove"]:
    state["plugins"] = [row for row in state["plugins"] if row["pluginId"] != args[2]]
    payload = {"success": True}
elif args[:3] == ["plugin", "marketplace", "remove"]:
    state["marketplaces"] = [
        row for row in state["marketplaces"] if row["name"] != args[3]
    ]
    payload = {"success": True}
else:
    payload = {"success": True}
state_path.write_text(json.dumps(state))
print(json.dumps(payload))
"""


def _run(container: ContainerHandle, *args: str) -> subprocess.CompletedProcess[str]:
    return container.exec(
        ["uv", "run", "setforge", *args, "--profile=mixed", f"--config={_CONFIG}"],
        check=False,
        env=_ENV,
    )


@pytest.mark.smoke
@pytest.mark.xdist_group("docker_daemon")
def test_mixed_codex_profile_converges_and_rolls_back_across_processes(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    container = docker_container()
    container.write_text(_CODEX_BIN, _FAKE_CODEX)
    container.exec(["chmod", "+x", _CODEX_BIN])
    container.exec(["mkdir", "-p", f"{_ROOT}/team"])
    container.write_text(f"{_ROOT}/tracked/codex/model.toml", 'model = "gpt-5"\n')
    container.write_text(
        f"{_ROOT}/tracked/claude/CLAUDE.md", "# Shared Claude instructions\n"
    )
    container.write_text(
        _CONFIG,
        "schema_version: '6.5'\n"
        "minimum_version: '6.4'\n"
        "version: 1\n"
        "tracked_files:\n"
        "  claude:\n"
        "    src: claude/CLAUDE.md\n"
        "    dst: ~/.codex-parity-claude/CLAUDE.md\n"
        "codex:\n"
        "  config:\n"
        "    model: {source: codex/model.toml}\n"
        "  marketplaces:\n"
        f"    team: {{source: path, path: {_ROOT}/team}}\n"
        "  plugins:\n"
        "    reviewer: {marketplace: team}\n"
        "profiles:\n"
        "  mixed:\n"
        "    tracked_files: [claude]\n"
        "    codex:\n"
        "      config: [model]\n"
        "      plugins: [reviewer]\n",
    )

    assert _run(container, "validate").returncode == 0
    installed = _run(container, "install", "--yes", "--no-secrets-scan")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert container.read_text(f"{_CODEX_HOME}/config.toml") == 'model = "gpt-5"\n'
    assert container.read_text("/home/tester/.codex-parity-claude/CLAUDE.md") == (
        "# Shared Claude instructions\n"
    )
    log = container.read_text(_CODEX_LOG)
    assert "plugin marketplace add /tmp/codex-parity/team --json" in log
    assert "plugin add reviewer@team --json" in log
    compared = _run(container, "compare", "--check")
    assert compared.returncode == 0, compared.stdout + compared.stderr

    repeated = _run(container, "install", "--yes", "--no-secrets-scan")
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert "noop" in repeated.stdout

    container.write_text(f"{_CODEX_HOME}/config.toml", 'model = "gpt-6"\n')
    assert _run(container, "compare", "--check").returncode == 1
    synced = _run(container, "sync", "--auto=use-live", "--yes")
    assert synced.returncode == 0, synced.stdout + synced.stderr
    assert container.read_text(f"{_ROOT}/tracked/codex/model.toml") == (
        'model = "gpt-6"\n'
    )

    reverted = _run(container, "revert", "--yes")
    assert reverted.returncode == 0, reverted.stdout + reverted.stderr
    assert container.read_text(f"{_ROOT}/tracked/codex/model.toml") == (
        'model = "gpt-5"\n'
    )
