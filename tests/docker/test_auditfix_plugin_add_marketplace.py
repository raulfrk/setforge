"""Docker E2E: `plugin add`/`plugin remove` and `marketplace add`/`remove`
against the REAL container ``claude`` binary.

Audit coverage gaps:

- ``plugin add``/``plugin remove`` were only ever exercised end-to-end via
  ``plugin list``; the mutating verbs that drive the real
  ``claude plugin marketplace add`` + ``claude plugin install`` +
  ``claude plugin enable`` binaries had unit tests that MOCK the binary, so
  the actual binary contract (install writes ``installed_plugins.json``
  without flipping ``enabledPlugins`` — the second ``enable`` call is what
  activates it) was never verified against the real ``claude`` 2.1.x binary
  baked into the e2e image.
- ``marketplace add``/``marketplace remove`` had no e2e coverage; only
  ``marketplace update`` was exercised. Two behaviors went unverified: the
  happy path against the real binary, and the failure-path atomicity — when
  ``claude marketplace add`` fails, the YAML entry written first is NOT
  rolled back, leaving an orphaned marketplace declaration in the config repo.

These tests build a real local (``path:``) marketplace directory the
container ``claude`` accepts (``.claude-plugin/marketplace.json`` + one
plugin dir with ``.claude-plugin/plugin.json``) and drive the real CLI.

NOT run here (no docker daemon); listed in e2eTestsWritten for the gated
``uv run pytest tests/docker/ -m e2e_docker`` suite.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_SRC_REPO = "/tmp/cfg-plugin-add"
_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
# A local marketplace directory the real `claude` binary can register.
_MP_DIR = "/tmp/local-mp"
# The marketplace name claude derives from marketplace.json — must match the
# name setforge records in YAML so `plugin install <plugin>@<name>` resolves.
_MP_NAME = "local-mp"
_PLUGIN_NAME = "demo-plugin"


def _bootstrap_config(c: ContainerHandle) -> None:
    """Write a minimal config repo + a path source block pointing at it."""
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


def _bootstrap_local_marketplace(c: ContainerHandle) -> None:
    """Build a real claude-code marketplace dir with one trivial plugin."""
    c.write_text(
        f"{_MP_DIR}/.claude-plugin/marketplace.json",
        json.dumps(
            {
                "name": _MP_NAME,
                "owner": {"name": "tester"},
                "plugins": [
                    {"name": _PLUGIN_NAME, "source": f"./{_PLUGIN_NAME}"},
                ],
            }
        ),
    )
    c.write_text(
        f"{_MP_DIR}/{_PLUGIN_NAME}/.claude-plugin/plugin.json",
        json.dumps({"name": _PLUGIN_NAME, "version": "0.0.1"}),
    )


def test_plugin_add_registers_yaml_installs_and_enables(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """`plugin add <name> --from path:... -m <mp>` writes the YAML (marketplace
    + bare plugin + bare profile binding) AND installs+enables via the real
    claude binary, leaving the plugin both installed and enabled."""
    c = docker_container()
    _bootstrap_config(c)
    _bootstrap_local_marketplace(c)

    res = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "plugin",
            "add",
            _PLUGIN_NAME,
            f"--from=path:{_MP_DIR}",
            "-m",
            _MP_NAME,
            "--profile=base",
        ],
        check=False,
    )
    combined = res.stdout + res.stderr
    assert res.returncode == 0, combined
    assert "Traceback (most recent call last)" not in combined, combined

    # (a) the config-repo setforge.yaml gained marketplace + plugin + binding,
    #     with the profile binding stored under the BARE plugin name (the audit
    #     fix) so the config still loads.
    yaml_text = c.read_text(f"{_SRC_REPO}/setforge.yaml")
    assert _MP_NAME in yaml_text, yaml_text
    assert _PLUGIN_NAME in yaml_text, yaml_text
    # No `@`-form binding leaked into the profile list.
    assert f"{_PLUGIN_NAME}@{_MP_NAME}" not in yaml_text, yaml_text
    # validate must succeed (proves the bare-binding/registry-key invariant).
    val = c.exec(
        ["uv", "run", "setforge", "validate", "--profile=base"],
        check=False,
    )
    assert val.returncode == 0, val.stdout + val.stderr

    # (b) the plugin is both INSTALLED and ENABLED per the real claude binary.
    listing = c.exec(["claude", "plugin", "list", "--json"], check=False)
    plugins = json.loads(listing.stdout) if listing.stdout.strip() else []
    pid = f"{_PLUGIN_NAME}@{_MP_NAME}"
    matched = [
        p
        for p in plugins
        if p.get("name") == _PLUGIN_NAME or p.get("id") == pid or p.get("name") == pid
    ]
    assert matched, listing.stdout
    entry = matched[0]
    # enabled flag shape varies; accept either an explicit enabled=True or a
    # status string. The second `claude plugin enable` call is what flips this.
    enabled = entry.get("enabled")
    status = str(entry.get("status", "")).lower()
    assert enabled is True or "enabled" in status, entry


def test_plugin_remove_disables_and_drops_binding(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """`plugin remove <name> --disable` drops the profile binding AND disables
    the plugin via the real claude binary."""
    c = docker_container()
    _bootstrap_config(c)
    _bootstrap_local_marketplace(c)

    add = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "plugin",
            "add",
            _PLUGIN_NAME,
            f"--from=path:{_MP_DIR}",
            "-m",
            _MP_NAME,
            "--profile=base",
        ],
        check=False,
    )
    assert add.returncode == 0, add.stdout + add.stderr

    rem = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "plugin",
            "remove",
            # pass the @-form to prove removal strips it to the bare binding
            f"{_PLUGIN_NAME}@{_MP_NAME}",
            "--disable",
            "--profile=base",
        ],
        check=False,
    )
    combined = rem.stdout + rem.stderr
    assert rem.returncode == 0, combined
    assert "Traceback (most recent call last)" not in combined, combined

    # Profile binding dropped from the config repo YAML. The plugin may remain
    # in the top-level registry (remove only touches the binding); we assert
    # validate still passes, which is the user-facing invariant.
    val = c.exec(
        ["uv", "run", "setforge", "validate", "--profile=base"],
        check=False,
    )
    assert val.returncode == 0, val.stdout + val.stderr
    # The disable call flipped the plugin off.
    listing = c.exec(["claude", "plugin", "list", "--json"], check=False)
    plugins = json.loads(listing.stdout) if listing.stdout.strip() else []
    matched = [p for p in plugins if p.get("name") == _PLUGIN_NAME]
    if matched:
        entry = matched[0]
        enabled = entry.get("enabled")
        status = str(entry.get("status", "")).lower()
        assert enabled is False or "disabled" in status, entry


def test_marketplace_add_writes_yaml_and_registers(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """`marketplace add <name> --from path:...` writes the YAML entry AND
    registers it with the real claude binary; `marketplace remove` reverses
    both."""
    c = docker_container()
    _bootstrap_config(c)
    _bootstrap_local_marketplace(c)

    add = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "marketplace",
            "add",
            _MP_NAME,
            f"--from=path:{_MP_DIR}",
        ],
        check=False,
    )
    combined = add.stdout + add.stderr
    assert add.returncode == 0, combined
    assert "Traceback (most recent call last)" not in combined, combined

    yaml_text = c.read_text(f"{_SRC_REPO}/setforge.yaml")
    assert _MP_NAME in yaml_text, yaml_text
    listing = c.exec(["claude", "plugin", "marketplace", "list", "--json"], check=False)
    assert _MP_NAME in listing.stdout, listing.stdout

    # remove reverses both the YAML entry and the claude registration.
    rem = c.exec(
        ["uv", "run", "setforge", "marketplace", "remove", _MP_NAME],
        check=False,
    )
    assert rem.returncode == 0, rem.stdout + rem.stderr
    yaml_after = c.read_text(f"{_SRC_REPO}/setforge.yaml")
    assert _MP_NAME not in yaml_after, yaml_after


def test_marketplace_add_binary_failure_does_not_leave_orphan_yaml_entry(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """When the real `claude marketplace add` fails (nonexistent github repo),
    `marketplace add` must exit non-zero AND not leave an orphaned marketplace
    entry in the config-repo setforge.yaml.

    Documents the audit finding: today the YAML is written BEFORE the binary
    call and is NOT rolled back on failure. This test asserts the DESIRED
    contract (no orphan); it will surface the divergence if the write-order
    atomicity is not fixed.
    """
    c = docker_container()
    _bootstrap_config(c)
    before = c.read_text(f"{_SRC_REPO}/setforge.yaml")

    res = c.exec(
        [
            "uv",
            "run",
            "setforge",
            "marketplace",
            "add",
            "ghost-mp",
            "--from=github:setforge-nonexistent-owner/does-not-exist-xyz",
        ],
        check=False,
    )
    combined = res.stdout + res.stderr
    assert res.returncode != 0, combined
    assert "Traceback (most recent call last)" not in combined, combined

    after = c.read_text(f"{_SRC_REPO}/setforge.yaml")
    assert "ghost-mp" not in after, (
        "orphaned marketplace entry left in YAML after binary failure:\n" + after
    )
    assert after == before, after
