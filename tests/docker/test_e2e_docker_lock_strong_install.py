"""Docker E2E: real-binary "strong install" coverage for the two lockfile
enforcement paths that :mod:`tests.docker.test_e2e_docker_lock` does not reach.

``test_e2e_docker_lock`` proves ``lock`` writes concrete pins and
``install --locked`` fails on a drifted checksum, but it stops short of
two behaviors that only bite through a real binary:

Test A — the PLUGIN sha-checkout (OFFLINE).
    Under ``claude.install_mode: local-clone`` a locked plugin's
    marketplace cache is hard-reset to the pinned 40-hex commit before
    ``claude plugin install`` runs
    (``PluginProvisioner._pin_marketplace`` →
    ``claude_marketplace_cache.checkout_marketplace_at``). The pin is
    resolved offline via ``git ls-remote`` against a local bare repo, so
    this case needs NO network and runs in the DEFAULT suite. The
    assertion reads the cache checkout's ``HEAD`` and proves it equals
    the pinned SHA — the checkout precedes ``plugin install`` in
    ``apply_one``, so the cache is reset even if the later
    ``claude plugin install`` call fails.

Test B — the VSIX strong-install (NETWORK-GATED).
    ``install --locked`` for an extension downloads the pinned VSIX,
    sha256-verifies it against the lock, and installs it from a temp
    ``.vsix`` via the real ``code --install-extension``
    (``vscode_extensions._install_pinned``). ``download_vsix`` always
    hits the marketplace URL and ``ResolvedPin`` carries no local-file
    source, so there is NO offline path — the case is gated behind
    ``SETFORGE_E2E_NETWORK`` like the other real-upstream lock cases.

Reused machinery (no parallel helpers invented):

- ``ContainerHandle`` (.exec/.write_text/.read_text) and the
  ``docker_container`` fixture from ``tests.docker.conftest``.
- ``_make_bare_marketplace_repo`` (a local **bare** git repo standing in
  for a github marketplace, carrying a resolvable HEAD) — adapted from
  ``tests.docker.test_e2e_docker_auditfix_plugin_synccache_e2e`` — plus
  that module's ``local.yaml`` install-mode wiring and the
  ``~/.cache/setforge/marketplaces/<repo-basename>`` cache-dir layout.
- The ``_NETWORK_ENV`` / ``_network_only`` (``SETFORGE_E2E_NETWORK``)
  marker, the ``code --list-extensions`` real-binary assertion, and the
  ``lock`` / ``install --locked`` invocation shape from
  ``tests.docker.test_e2e_docker_lock``.
"""

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

# ---------------------------------------------------------------------------
# Test A — plugin sha-checkout (OFFLINE)
# ---------------------------------------------------------------------------

_SRC_REPO_A = "/tmp/cfg-strong-plugin"
_CONFIG_A = f"{_SRC_REPO_A}/setforge.yaml"
_PROFILE_A = "strong-plugin"
_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"

# The local bare repo that stands in for a github-backed marketplace. Its
# basename (``mp-strong.git``) is the cache-dir subdir name under LOCAL_CLONE,
# matching ``_source_identity``'s ``cache_root / <repo-basename>`` layout.
_BARE_REPO = "/tmp/mp-strong.git"
_BARE_BASENAME = "mp-strong.git"
_CACHE_DIR = f"/home/tester/.cache/setforge/marketplaces/{_BARE_BASENAME}"

# A github-backed marketplace whose ``repo`` is the local bare repo path
# (``git clone`` / ``git ls-remote`` accept a filesystem path verbatim), a
# plugin referencing it, and a profile pulling the plugin in. The 6.0 shape:
# plugin flows through a top-level ``packages`` entry; the ``claude_plugins``
# registry maps plugin name -> marketplace.
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
    """Create a local **bare** git repo at ``_BARE_REPO`` and return its HEAD SHA.

    Adapted from
    ``tests.docker.test_e2e_docker_auditfix_plugin_synccache_e2e``. Builds
    a normal repo with one real commit, clones it ``--bare`` so the bare
    repo carries a resolvable ``HEAD`` (an empty ``git init --bare`` has no
    commits, so a later ``git ls-remote``/refresh would find no ref), then
    returns the 40-hex commit SHA that the plugin pin must lock to.
    """
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
    """Write ``local.yaml`` selecting ``claude.install_mode: local-clone``.

    ``install_mode`` is read from the fixed ``~/.config/setforge/local.yaml``
    (``binaries.LOCAL_CONFIG_PATH``), independent of ``--config``, so no
    ``source:`` block is needed — this test drives the config via ``--config``.
    """
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
    """A locked plugin under local-clone hard-resets its marketplace cache to
    the pinned commit SHA before install — proven OFFLINE against a local bare
    repo.

    ``setforge lock`` resolves the plugin's commit via ``git ls-remote``
    against the local bare repo (no network), and ``install --locked``
    under ``install_mode: local-clone`` clones the marketplace into the
    cache and hard-resets it to that pin
    (``checkout_marketplace_at``). The assertion reads the cache
    checkout's ``HEAD`` and proves it equals the pinned SHA. The checkout
    precedes ``claude plugin install`` in ``apply_one``, so this holds
    even if the later real ``claude`` install call fails.
    """
    c = docker_container()
    pinned_sha = _make_bare_marketplace_repo(c)
    _write_plugin_source(c)
    _set_local_clone_mode(c)

    lock_res = _lock_a(c)
    assert lock_res.returncode == 0, lock_res.stdout + lock_res.stderr

    # The lock must record the resolved plugin commit as the pin — a 40-hex
    # SHA equal to the bare repo's HEAD, never a moving ref.
    lock_text = c.read_text(f"{_SRC_REPO_A}/setforge.lock")
    assert pinned_sha in lock_text, (
        f"lock did not record the resolved plugin SHA {pinned_sha}:\n{lock_text}"
    )

    _install_locked_a(c)

    # The cache checkout must be hard-reset to the pinned SHA — this is the
    # strong-install guarantee that unit tests (mocked git) cannot prove.
    head = c.exec(["git", "-C", _CACHE_DIR, "rev-parse", "HEAD"], check=False)
    assert head.returncode == 0, (
        f"marketplace cache checkout missing at {_CACHE_DIR}: "
        f"{head.stdout}{head.stderr}"
    )
    assert head.stdout.strip() == pinned_sha, (
        f"cache HEAD {head.stdout.strip()!r} != pinned SHA {pinned_sha!r}; "
        f"the pin was not enforced"
    )


# ---------------------------------------------------------------------------
# Test B — VSIX strong-install (NETWORK-GATED)
# ---------------------------------------------------------------------------

_SRC_REPO_B = "/tmp/cfg-strong-vsix"
_CONFIG_B = f"{_SRC_REPO_B}/setforge.yaml"
_PROFILE_B = "strong-vsix"
_EXT_ID = "esbenp.prettier-vscode"

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
    """``install --locked`` for a locked extension downloads + sha256-verifies
    the pinned VSIX and installs it from a temp ``.vsix`` via real ``code``.

    Network-gated: ``download_vsix`` always fetches the marketplace URL
    (no local-file source on ``ResolvedPin``), so the from-file
    ``_install_pinned`` path cannot run offline. The assertion queries the
    real ``code --list-extensions`` binary (baked into the e2e image) to
    prove the extension is installed.
    """
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
