"""Docker E2E: ``plugin sync-cache`` branches + local-clone cache-miss remediation.

Closes the e2e-coverage gap flagged by the round-4 audit: an exhaustive
grep of all docker e2e files (``rg 'sync-cache' tests/docker``) returned
ZERO matches, so the documented offline-install prerequisite
(``setforge plugin sync-cache``) and its companion cache-miss failure
mode were never exercised in a real container. The engine functions are
unit-tested with mocked/local git (``tests/test_claude_marketplace_cache.py``),
but those cannot prove the real clone-into-cache + offline-install
handoff that the e2e suite exists to gate.

All cases are network-free. A github-backed marketplace is faked with a
local **bare** git repo (``/tmp/mp-origin.git``) whose path is used
verbatim as ``MarketplaceSource.repo`` — ``_clone_marketplace`` runs
``git clone -- <repo> <cache_dir>``, which accepts a local filesystem
path exactly like a remote URL. The cache dir basename is the repo
basename (``mp-origin.git``), so the on-disk mirror lands at
``~/.cache/setforge/marketplaces/mp-origin.git``.

Cases:

(1) ``plugin sync-cache`` under default (regular) install mode →
    short-circuit warning + exit 0, no cache dir created.
(2) ``plugin sync-cache`` under local-clone whose only referenced
    marketplace is a PATH source → "no GitHub-backed marketplaces"
    message + exit 0.
(3) ``plugin sync-cache`` under local-clone with a github-backed
    marketplace (local bare repo) → exit 0, "refreshed <mp>" line, and
    the cache dir now exists.
(4) ``install`` under local-clone with the cache deliberately ABSENT and
    the clone source unreachable → the MarketplaceCacheMiss remediation
    surfaces naming ``sync-cache``, with no traceback escaping. (The
    cache miss surfaces as a per-marketplace FAILED reconcile line; only
    MCP failures gate the install exit code, so install itself still
    completes — the contract under test is the remediation message, not
    a non-zero exit.)
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.docker.conftest import ContainerHandle

pytestmark = pytest.mark.e2e_docker

_SRC_REPO = "/tmp/cfg-synccache"
_HOME_LOCAL_YAML = "/home/tester/.config/setforge/local.yaml"
_PROFILE = "base"
_BARE_REPO = "/tmp/mp-origin.git"
_BARE_BASENAME = "mp-origin.git"
_CACHE_DIR = f"/home/tester/.cache/setforge/marketplaces/{_BARE_BASENAME}"


def _point_source_at(c: ContainerHandle, *, install_mode: str | None = None) -> None:
    """Point local.yaml's source at ``_SRC_REPO`` and (optionally) set install_mode."""
    body = f"source:\n  kind: path\n  path: {_SRC_REPO}\n"
    if install_mode is not None:
        body += f"claude:\n  install_mode: {install_mode}\n"
    c.write_text(_HOME_LOCAL_YAML, body)


def _write_config(c: ContainerHandle, *, body: str) -> None:
    """Write a minimal config repo (setforge.yaml + a tracked file)."""
    c.write_text(f"{_SRC_REPO}/setforge.yaml", body)
    c.write_text(f"{_SRC_REPO}/tracked/foo.md", "# foo\n")


def _make_bare_marketplace_repo(c: ContainerHandle) -> None:
    """Create a local **bare** git repo at ``_BARE_REPO`` to stand in for a
    github-backed marketplace.

    Build a normal repo with one commit, then clone it ``--bare`` so the
    bare repo carries a resolvable ``HEAD`` (an empty ``git init --bare``
    has no commits, so ``git clone`` of it produces a cache with no
    ``origin/HEAD`` and a later refresh would fail). The bare repo only
    needs to be *clonable* for sync-cache — sync-cache clones, it does not
    invoke ``claude plugin marketplace add``, so no marketplace manifest
    is required.
    """
    seed = "/tmp/mp-seed"
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


# ---------------------------------------------------------------------------
# Config bodies
# ---------------------------------------------------------------------------

# A github-backed marketplace whose `repo` is the local bare repo path,
# plus a plugin that references it so the profile's claude_plugins pulls
# the marketplace into sync-cache's referenced set.
_GITHUB_MP_YAML = f"""\
version: 1
schema_version: '1.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
marketplaces:
  fixture-mp:
    source: github
    repo: {_BARE_REPO}
claude_plugins:
  some-plugin:
    marketplace: fixture-mp
profiles:
  base:
    tracked_files:
      - foo
    claude_plugins:
      - some-plugin
"""

# A profile whose only referenced marketplace is a PATH source — exercises
# the "no GitHub-backed marketplaces in profile" short-circuit.
_PATH_MP_YAML = """\
version: 1
schema_version: '1.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
marketplaces:
  path-mp:
    source: path
    path: /tmp/some-local-mp
claude_plugins:
  some-plugin:
    marketplace: path-mp
profiles:
  base:
    tracked_files:
      - foo
    claude_plugins:
      - some-plugin
"""

# A github-backed marketplace pointing at a path that does NOT exist, so a
# clone-on-install attempt fails — exercises the cache-miss remediation.
_MISSING_MP_YAML = """\
version: 1
schema_version: '1.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
marketplaces:
  fixture-mp:
    source: github
    repo: /tmp/does-not-exist.git
claude_plugins:
  some-plugin:
    marketplace: fixture-mp
profiles:
  base:
    tracked_files:
      - foo
    claude_plugins:
      - some-plugin
"""


def _sync_cache(c: ContainerHandle, *, check: bool = True):
    return c.exec(
        ["uv", "run", "setforge", "plugin", "sync-cache", f"--profile={_PROFILE}"],
        check=check,
    )


# ---------------------------------------------------------------------------
# (1) regular mode → warn + exit 0, no-op
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_sync_cache_regular_mode_warns_no_op(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """Default (regular) install mode: sync-cache warns and touches nothing."""
    c = docker_container()
    _write_config(c, body=_GITHUB_MP_YAML)
    _point_source_at(c)  # no install_mode → regular default

    res = _sync_cache(c, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "install_mode is 'regular'" in res.stderr, res.stderr
    # Short-circuit happens before any cache work: the dir was never made.
    probe = c.exec(["test", "-d", _CACHE_DIR], check=False)
    assert probe.returncode != 0, f"cache dir unexpectedly created: {_CACHE_DIR}"


# ---------------------------------------------------------------------------
# (2) local-clone, only PATH marketplace → "no GitHub-backed marketplaces"
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_sync_cache_path_only_reports_no_github(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """local-clone with a PATH-only marketplace: the no-github message + exit 0."""
    c = docker_container()
    _write_config(c, body=_PATH_MP_YAML)
    _point_source_at(c, install_mode="local-clone")

    res = _sync_cache(c, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    combined = res.stdout + res.stderr
    assert "no GitHub-backed marketplaces in profile" in combined, combined


# ---------------------------------------------------------------------------
# (3) local-clone, github marketplace → clone into cache + "refreshed"
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_sync_cache_clones_github_marketplace_into_cache(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """local-clone with a github-backed marketplace: clone lands in the cache.

    The github source is a local bare repo, so ``git clone`` succeeds
    offline. Asserts exit 0, the per-marketplace "refreshed" line, and
    that the cache mirror now exists on disk.
    """
    c = docker_container()
    _make_bare_marketplace_repo(c)
    _write_config(c, body=_GITHUB_MP_YAML)
    _point_source_at(c, install_mode="local-clone")

    # Cache absent before the first sync-cache.
    pre = c.exec(["test", "-d", _CACHE_DIR], check=False)
    assert pre.returncode != 0, f"cache dir present before sync-cache: {_CACHE_DIR}"

    res = _sync_cache(c, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    combined = res.stdout + res.stderr
    assert "refreshed fixture-mp" in combined, combined
    # Cache mirror now on disk and is a real git checkout.
    post = c.exec(["test", "-d", f"{_CACHE_DIR}/.git"], check=False)
    assert post.returncode == 0, f"cache git checkout missing at {_CACHE_DIR}"

    # Second sync-cache hits the refresh (fetch + reset) branch, not clone,
    # and still succeeds idempotently.
    res2 = _sync_cache(c, check=False)
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "refreshed fixture-mp" in (res2.stdout + res2.stderr)
    assert "Traceback (most recent call last)" not in (res2.stdout + res2.stderr)


# ---------------------------------------------------------------------------
# (4) install under local-clone, cache absent + unreachable source →
#     MarketplaceCacheMiss remediation pointing at sync-cache
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_install_local_clone_cache_miss_points_to_sync_cache(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """install under local-clone with no cache + unreachable repo surfaces the
    cache-miss remediation naming sync-cache, without a traceback.

    The marketplace cache is absent and its github source points at a
    nonexistent path, so the on-demand clone fails and raises
    MarketplaceCacheMiss. That surfaces as a per-marketplace reconcile
    FAILED line carrying the category-aware remediation (a missing repo
    path classifies as "repository not found" and names sync-cache). The
    cache-miss does NOT gate the install exit code (only MCP failures
    do), so the contract under test is the remediation message + no
    traceback escaping.
    """
    c = docker_container()
    _write_config(c, body=_MISSING_MP_YAML)
    _point_source_at(c, install_mode="local-clone")

    res = c.exec(
        ["uv", "run", "setforge", "install", f"--profile={_PROFILE}", "--yes"],
        check=False,
    )
    combined = res.stdout + res.stderr
    # The remediation must name sync-cache so the user knows the fix.
    assert "sync-cache" in combined, combined
    # And it must be the cache-miss remediation: a missing repo path is
    # classified as "repository not found" (NOT mislabeled offline).
    assert "repository not found" in combined.lower(), combined
    assert "offline" not in combined.lower(), combined
    # No unhandled traceback should escape the graceful-degradation path.
    assert "Traceback (most recent call last)" not in combined, combined


# ---------------------------------------------------------------------------
# (5) local-clone, github SHORTHAND marketplace → bare `owner/repo` is
#     expanded to a full https://github.com/owner/repo URL before cloning.
#     The m97t regression guard. Network-free via a git `insteadOf` rewrite.
# ---------------------------------------------------------------------------

# A github-backed marketplace whose `repo` is a real `owner/repo` SHORTHAND
# (NOT a local path) — the shape the author's real config uses. Cases 1-4 fake
# github with a local bare-repo path, which `git clone` accepts verbatim and so
# never exercised the shorthand→URL expansion that m97t fixes.
_SHORTHAND_REPO = "testorg/testmp"
_SHORTHAND_BASENAME = "testmp"
_SHORTHAND_CACHE_DIR = (
    f"/home/tester/.cache/setforge/marketplaces/{_SHORTHAND_BASENAME}"
)
_SHORTHAND_MP_YAML = f"""\
version: 1
schema_version: '1.0'
tracked_files:
  foo:
    src: foo.md
    dst: /tmp/out/foo.md
marketplaces:
  fixture-mp:
    source: github
    repo: {_SHORTHAND_REPO}
claude_plugins:
  some-plugin:
    marketplace: fixture-mp
profiles:
  base:
    tracked_files:
      - foo
    claude_plugins:
      - some-plugin
"""


def _rewrite_github_url_to_local(
    c: ContainerHandle, *, shorthand: str, local: str
) -> None:
    """Make ``https://github.com/<shorthand>`` resolve to a local bare repo.

    A global git ``insteadOf`` rewrite keeps the test network-free while still
    forcing the real code path: setforge must build the full
    ``https://github.com/<shorthand>`` URL for the rewrite to match. The
    pre-fix code passed the bare ``<shorthand>`` verbatim, which does NOT match
    this rewrite and fails — so this test passes ONLY with the m97t fix.
    """
    c.exec(
        [
            "git",
            "config",
            "--global",
            f"url.{local}.insteadOf",
            f"https://github.com/{shorthand}",
        ],
        check=True,
    )


@pytest.mark.xdist_group("docker_daemon")
def test_e2e_docker_sync_cache_expands_github_shorthand_to_url(
    docker_container: Callable[..., ContainerHandle],
) -> None:
    """A bare ``owner/repo`` shorthand is expanded to a full HTTPS URL before
    cloning — the m97t regression guard.

    Real git rejects a bare ``owner/repo`` ("does not appear to be a git
    repository") even when online, because only ``claude`` / ``gh`` understand
    the shorthand. setforge must prepend ``https://github.com/``. Proven
    network-free: a git ``insteadOf`` rewrite maps the EXPANDED
    ``https://github.com/testorg/testmp`` URL to a local bare repo, so the
    clone succeeds iff setforge built the full URL. The pre-fix bare-shorthand
    clone would not match the rewrite and would fail.
    """
    c = docker_container()
    _make_bare_marketplace_repo(c)  # local bare repo at _BARE_REPO
    _rewrite_github_url_to_local(c, shorthand=_SHORTHAND_REPO, local=_BARE_REPO)
    _write_config(c, body=_SHORTHAND_MP_YAML)
    _point_source_at(c, install_mode="local-clone")

    res = _sync_cache(c, check=False)
    # The clone succeeds ONLY because setforge expanded the bare shorthand to
    # `https://github.com/testorg/testmp`, which the insteadOf rewrite then
    # maps to the local bare repo. The pre-fix code passed the bare shorthand
    # verbatim — no rewrite match — and would fail here. This is the m97t guard.
    assert res.returncode == 0, res.stdout + res.stderr
    combined = res.stdout + res.stderr
    assert "refreshed fixture-mp" in combined, combined
    assert "Traceback (most recent call last)" not in combined, combined
    # The cache mirror landed on disk under the shorthand basename.
    post = c.exec(["test", "-d", f"{_SHORTHAND_CACHE_DIR}/.git"], check=False)
    assert post.returncode == 0, f"cache git checkout missing at {_SHORTHAND_CACHE_DIR}"
    # NB: a second sync-cache is intentionally NOT exercised here. The insteadOf
    # rewrite makes git record the LOCAL path as the cache's origin, so the
    # drift check would see it differ from the declared `testorg/testmp` — a
    # fixture artifact, not real behavior (a real clone records the github URL).
    # Refresh idempotency is covered by the local-path marketplace in case (3).
