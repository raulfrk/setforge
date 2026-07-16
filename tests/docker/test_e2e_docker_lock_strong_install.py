from __future__ import annotations

import os
import re
from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_NETWORK_ENV = "SETFORGE_E2E_NETWORK"
_network_only = pytest.mark.skipif(
    os.environ.get(_NETWORK_ENV, "") == "",
    reason=f"set {_NETWORK_ENV}=1 to run the real lock/marketplace network cases",
)

_SRC_REPO_A = "/tmp/cfg-strong-plugin"
_CONFIG_A = f"{_SRC_REPO_A}/setforge.yaml"
_PROFILE_A = "strong-plugin"
_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"

_BARE_REPO = "/tmp/mp-strong.git"
_BARE_BASENAME = "mp-strong.git"
_CACHE_DIR = f"/home/tester/.cache/setforge/marketplaces/{_BARE_BASENAME}"

_PLUGIN_CONFIG_YAML = f"""\
version: 1
schema_version: '6.0'
tracked_files:
  note:
    src: note.md
    dst: /tmp/strong-out/note.md
marketplaces:
  fixture-mp:
    source: github
    repo: {_BARE_REPO}
claude_plugins:
  some-plugin:
    marketplace: fixture-mp
packages:
  some-plugin:
    type: plugin
    plugin: some-plugin
profiles:
  {_PROFILE_A}:
    tracked_files:
      - note
    packages:
      - some-plugin
"""


def _make_bare_marketplace_repo(c: ContainerHandle) -> str:
    """Create a local **bare** git repo at ``_BARE_REPO`` and return its HEAD SHA."""
    seed = "/tmp/mp-strong-seed"
    script = (
        f"set -e; "
        f"rm -rf {seed} {_BARE_REPO}; "
        f"git init -q {seed}; "
        f"cd {seed}; "
        f"git config user.email t@e.x; git config user.name t; "
        f"echo manifest > marketplace.json; "
        f"git add -A; git commit -q -m seed; "
        f"git clone -q --bare {seed} {_BARE_REPO}"
    )
    c.exec(["sh", "-c", script], check=True)
    head = c.exec(["git", "-C", seed, "rev-parse", "HEAD"], check=True)
    sha = head.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{40}", sha), f"seed HEAD is not a 40-hex SHA: {sha!r}"
    return sha


def _write_plugin_source(c: ContainerHandle) -> None:
    c.write_text(_CONFIG_A, _PLUGIN_CONFIG_YAML)
    c.write_text(f"{_SRC_REPO_A}/tracked/note.md", "# note\n")


def _set_local_clone_mode(c: ContainerHandle) -> None:
    c.write_text(_LOCAL_YAML, "claude:\n  install_mode: local-clone\n")


def _lock_a(c: ContainerHandle, *, check: bool = False):
    return c.exec(
        [
            "uv",
            "run",
            "setforge",
            "lock",
            f"--profile={_PROFILE_A}",
            f"--config={_CONFIG_A}",
        ],
        check=check,
    )


def _install_locked_a(c: ContainerHandle, *, check: bool = False):
    return c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--locked",
            "--yes",
            f"--profile={_PROFILE_A}",
            f"--config={_CONFIG_A}",
        ],
        check=check,
    )


@pytest.mark.xdist_group("docker_daemon")
def test_install_locked_plugin_hard_resets_cache_to_pinned_sha(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """The cache checkout precedes ``claude plugin install`` in ``apply_one``,
    so the hard-reset to the pinned SHA holds even if that call fails."""
    c = docker_container()
    pinned_sha = _make_bare_marketplace_repo(c)
    _write_plugin_source(c)
    _set_local_clone_mode(c)

    lock_res = _lock_a(c)
    assert lock_res.returncode == 0, lock_res.stdout + lock_res.stderr

    lock_text = c.read_text(f"{_SRC_REPO_A}/setforge.lock")
    assert pinned_sha in lock_text, (
        f"lock did not record the resolved plugin SHA {pinned_sha}:\n{lock_text}"
    )

    _install_locked_a(c)

    head = c.exec(["git", "-C", _CACHE_DIR, "rev-parse", "HEAD"], check=False)
    assert head.returncode == 0, (
        f"marketplace cache checkout missing at {_CACHE_DIR}: "
        f"{head.stdout}{head.stderr}"
    )
    assert head.stdout.strip() == pinned_sha, (
        f"cache HEAD {head.stdout.strip()!r} != pinned SHA {pinned_sha!r}; "
        f"the pin was not enforced"
    )


_SRC_REPO_B = "/tmp/cfg-strong-vsix"
_CONFIG_B = f"{_SRC_REPO_B}/setforge.yaml"
_PROFILE_B = "strong-vsix"
_EXT_ID = "tomoki1207.pdf"

_VSIX_CONFIG_YAML = f"""\
version: 1
schema_version: '6.0'
tracked_files:
  note:
    src: note.md
    dst: /tmp/strong-out/note.md
packages:
  {_EXT_ID}:
    type: extension
    extension: {_EXT_ID}
profiles:
  {_PROFILE_B}:
    tracked_files:
      - note
    packages:
      - {_EXT_ID}
"""


def _write_vsix_source(c: ContainerHandle) -> None:
    c.write_text(_CONFIG_B, _VSIX_CONFIG_YAML)
    c.write_text(f"{_SRC_REPO_B}/tracked/note.md", "# note\n")


def _lock_b(c: ContainerHandle, *, check: bool = False):
    return c.exec(
        [
            "uv",
            "run",
            "setforge",
            "lock",
            f"--profile={_PROFILE_B}",
            f"--config={_CONFIG_B}",
        ],
        check=check,
    )


def _install_locked_b(c: ContainerHandle, *, check: bool = False):
    return c.exec(
        [
            "uv",
            "run",
            "setforge",
            "install",
            "--locked",
            "--yes",
            f"--profile={_PROFILE_B}",
            f"--config={_CONFIG_B}",
        ],
        check=check,
    )


@_network_only
@pytest.mark.xdist_group("docker_daemon")
def test_install_locked_extension_installs_verified_vsix_via_code(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    c = docker_container()
    _write_vsix_source(c)

    lock_res = _lock_b(c)
    assert lock_res.returncode == 0, lock_res.stdout + lock_res.stderr

    install_res = _install_locked_b(c)
    assert install_res.returncode == 0, install_res.stdout + install_res.stderr

    listed = c.exec(["code", "--list-extensions"], check=False)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    installed = {line.strip().lower() for line in listed.stdout.splitlines()}
    assert _EXT_ID.lower() in installed, (
        f"{_EXT_ID} not shown by `code --list-extensions`:\n{listed.stdout}"
    )
