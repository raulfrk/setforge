"""Tests for marketplace + git + cache plumbing (``setforge.claude_marketplace_cache``).

Exercises ``resolve_marketplace_source`` (install-mode dispatch),
``_clone_marketplace`` argv hygiene, ``_safe_cache_dir`` path-traversal
guards, and ``sync_marketplace_cache`` semantics. The ``fake_git``
fixture (defined in :mod:`tests.conftest`) wires :class:`FakeGit` into
the new module's ``subprocess`` / ``shutil`` namespace so
monkeypatch paths track the split.
"""

import subprocess
from pathlib import Path

import pytest

from setforge.config import (
    ClaudeInstallMode,
    ClaudePluginRef,
    MarketplaceSource,
    MarketplaceSourceKind,
)
from tests.conftest import _make_config, _make_resolved

# ---------------------------------------------------------------------------
# resolve_marketplace_source (pure transform)
# ---------------------------------------------------------------------------


def test_readd_after_failed_registration_uses_legacy_alias_sidecar(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache-root alias sidecar written by an earlier version keeps steering a
    colliding repo to its own subdir: no collision error, no new clone, and the
    colliding basename directory is left alone."""
    import json

    from setforge import claude_marketplace_cache as cache
    from setforge import claude_plugins

    fake = fake_git(known_repos={"alice/tools", "bob/tools"})
    root = tmp_path / "marketplaces"
    original = root / "tools"
    original.mkdir(parents=True)
    (original / "keep").write_text("unrelated cache")
    fake.cloned[original] = "alice/tools"
    alias = root / "tools-bob"
    alias.mkdir()
    fake.cloned[alias] = "bob/tools"
    (root / ".aliases.json").write_text(json.dumps({"bob/tools": "tools-bob"}))
    monkeypatch.setattr(claude_plugins, "_get_claude_bin", lambda: Path("claude"))
    source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="bob/tools")

    plan = cache.plan_marketplace_source(
        source, ClaudeInstallMode.LOCAL_CLONE, cache_root=root
    )

    assert cache.read_cache_aliases(root) == {"bob/tools": "tools-bob"}
    assert plan.cache_dir == alias
    assert plan.action is cache.MarketplaceSourceAction.NONE
    effective = cache.apply_marketplace_source_plan(plan)
    assert effective.path == alias
    registrations: list[list[str]] = []

    def register(argv: list[str]) -> subprocess.CompletedProcess[str]:
        registrations.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(claude_plugins, "_run_claude", register)
    claude_plugins.marketplace_add("tools", effective)
    assert registrations[0][-1] == str(alias)
    assert fake.clone_count() == 0
    assert (original / "keep").read_text() == "unrelated cache"
    assert fake.cloned[original] == "alice/tools"


def testresolve_marketplace_source_regular_returns_input(tmp_path: Path) -> None:
    """REGULAR mode never touches the source — pure passthrough."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source

    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    out = resolve_marketplace_source(
        src, ClaudeInstallMode.REGULAR, cache_root=tmp_path
    )
    assert out is src
    assert not any(tmp_path.iterdir())  # no cache I/O


def testresolve_marketplace_source_path_kind_passthrough(tmp_path: Path) -> None:
    """PATH sources passthrough regardless of mode (already local)."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source

    local = tmp_path / "preinstalled"
    local.mkdir()
    src = MarketplaceSource(source=MarketplaceSourceKind.PATH, path=local)
    out = resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=tmp_path / "cache"
    )
    assert out is src


def testresolve_marketplace_source_local_clone_clones_on_cache_miss(
    fake_git, tmp_path: Path
) -> None:
    """Cache miss in LOCAL_CLONE mode triggers a single git clone."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source

    fake = fake_git(known_repos={"anthropic/plug"})
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    out = resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=tmp_path / "cache"
    )
    assert out.source is MarketplaceSourceKind.PATH
    assert out.path == tmp_path / "cache" / "plug"
    assert fake.clone_count() == 1


def test_marketplace_source_plan_defers_clone_until_apply(
    fake_git, tmp_path: Path
) -> None:
    """Install planning fixes the cache target without creating it."""
    from setforge.claude_marketplace_cache import (
        apply_marketplace_source_plan,
        plan_marketplace_source,
    )

    fake = fake_git(known_repos={"anthropic/plug"})
    cache_root = tmp_path / "cache"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")

    plan = plan_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root
    )

    assert fake.clone_count() == 0
    assert not cache_root.exists()
    assert apply_marketplace_source_plan(plan).path == cache_root / "plug"
    assert fake.clone_count() == 1


def test_marketplace_source_plan_refuses_cache_created_after_planning(
    fake_git, tmp_path: Path
) -> None:
    """A stale clone decision cannot overwrite a cache created concurrently."""
    from setforge.claude_marketplace_cache import (
        apply_marketplace_source_plan,
        plan_marketplace_source,
    )
    from setforge.errors import MarketplaceCacheMiss

    fake = fake_git(known_repos={"anthropic/plug"})
    cache_root = tmp_path / "cache"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    plan = plan_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root
    )
    (cache_root / "plug").mkdir(parents=True)

    with pytest.raises(MarketplaceCacheMiss, match="changed after planning"):
        apply_marketplace_source_plan(plan)
    assert fake.clone_count() == 0


def test_marketplace_source_plan_refuses_origin_changed_after_planning(
    fake_git, tmp_path: Path
) -> None:
    """An existing cache must retain the origin observed by the plan."""
    from setforge.claude_marketplace_cache import (
        plan_marketplace_source,
        validate_marketplace_source_plan,
    )
    from setforge.errors import MarketplaceCacheMiss

    fake = fake_git(known_repos={"anthropic/plug"})
    cache_root = tmp_path / "cache"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    fake.cloned[cache_dir] = "anthropic/plug"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    plan = plan_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root
    )
    fake.cloned[cache_dir] = "other/plug"

    with pytest.raises(MarketplaceCacheMiss, match="origin changed"):
        validate_marketplace_source_plan(plan)


def test_marketplace_plan_source_snapshots_are_detached(tmp_path: Path) -> None:
    from setforge.claude_marketplace_cache import plan_marketplace_source

    source = MarketplaceSource(
        source=MarketplaceSourceKind.PATH, path=tmp_path / "before"
    )
    plan = plan_marketplace_source(source, ClaudeInstallMode.LOCAL_CLONE)

    source.path = tmp_path / "after"
    detached = plan.effective_source
    detached.path = tmp_path / "also-after"

    assert plan.source.path == tmp_path / "before"
    assert plan.effective_source.path == tmp_path / "before"


def testresolve_marketplace_source_local_clone_offline_raises(
    fake_git, tmp_path: Path
) -> None:
    """git clone failure surfaces as MarketplaceCacheMiss with remediation."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source
    from setforge.errors import MarketplaceCacheMiss

    fake_git(known_repos=set())  # any clone fails
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    with pytest.raises(MarketplaceCacheMiss, match="sync-cache"):
        resolve_marketplace_source(
            src, ClaudeInstallMode.LOCAL_CLONE, cache_root=tmp_path / "cache"
        )


def testresolve_marketplace_source_git_binary_missing_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing git binary yields a specific remediation message."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source
    from setforge.errors import MarketplaceCacheMiss

    monkeypatch.setattr(
        "setforge.claude_marketplace_cache.shutil.which",
        lambda _: None,
    )
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    with pytest.raises(MarketplaceCacheMiss, match=r"git.*not on PATH"):
        resolve_marketplace_source(
            src, ClaudeInstallMode.LOCAL_CLONE, cache_root=tmp_path / "cache"
        )


def testresolve_marketplace_source_existing_cache_no_clone(
    fake_git, tmp_path: Path
) -> None:
    """When the cache already exists with a matching origin, no git clone runs."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source

    fake = fake_git(known_repos={"anthropic/plug"})
    cache_root = tmp_path / "cache"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    # Pre-register the origin URL so _cache_origin_url returns a match.
    fake.cloned[cache_dir] = "anthropic/plug"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    out = resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root
    )
    assert out.path == cache_dir
    assert fake.clone_count() == 0


def testresolve_marketplace_source_origin_probe_failure_reuses_cache(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed origin probe reuses the cache as-is — no wizard, no clone.

    ``_cache_origin_url`` is best-effort and returns ``None`` on any git
    failure (no remote, corrupted checkout); the resolver must fall
    through to the existing cache rather than treat the miss as URL
    drift.
    """
    from setforge import claude_marketplace_cache as mp_cache
    from setforge.claude_marketplace_cache import resolve_marketplace_source

    fake = fake_git(known_repos={"anthropic/plug"})
    cache_root = tmp_path / "cache"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    # Origin probe fails (e.g. remote unset): _cache_origin_url -> None.
    monkeypatch.setattr(mp_cache, "_cache_origin_url", lambda _cache_dir: None)
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    out = resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root
    )
    assert out.source is MarketplaceSourceKind.PATH
    assert out.path == cache_dir
    assert fake.clone_count() == 0


def testresolve_marketplace_source_url_drift_raises_and_changes_nothing(
    fake_git, tmp_path: Path
) -> None:
    """A cache dir holding another repo is refused with the manual steps.

    The error names the marketplace, the cache directory, the clone's origin and
    the declared repo, and gives both ways out; the cache is left as it was and
    nothing is cloned.
    """
    from setforge.claude_marketplace_cache import resolve_marketplace_source
    from setforge.errors import MarketplaceCacheMiss

    fake = fake_git(known_repos={"anthropic/plug", "newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    (cache_dir / "marker.txt").write_text("existing clone")
    fake.cloned[cache_dir] = "anthropic/plug"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")

    with pytest.raises(MarketplaceCacheMiss) as raised:
        resolve_marketplace_source(
            src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root, mp_name="mine"
        )

    message = str(raised.value)
    assert "marketplace 'mine'" in message
    assert str(cache_dir) in message
    assert "'anthropic/plug'" in message
    assert "'newowner/plug'" in message
    assert "Nothing was changed" in message
    assert "set this marketplace's repo in setforge.yaml to 'anthropic/plug'" in message
    assert f"rm -rf {cache_dir}" in message
    assert (cache_dir / "marker.txt").read_text() == "existing clone"
    assert fake.cloned == {cache_dir: "anthropic/plug"}
    assert fake.clone_count() == 0
    assert not any("fetch" in c or "reset" in c for c in fake.calls)


def test_url_drift_error_quotes_a_cache_path_with_spaces(
    fake_git, tmp_path: Path
) -> None:
    """The ``rm -rf`` step in the error is safe to paste when the path has spaces."""
    from setforge.claude_marketplace_cache import plan_marketplace_source
    from setforge.errors import MarketplaceCacheMiss

    fake = fake_git(known_repos=set())
    cache_root = tmp_path / "my cache"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    fake.cloned[cache_dir] = "anthropic/plug"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")

    with pytest.raises(MarketplaceCacheMiss) as raised:
        plan_marketplace_source(
            src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root
        )

    assert f"rm -rf '{cache_dir}'" in str(raised.value)


# ---------------------------------------------------------------------------
# _clone_marketplace argv hygiene (`--` separator)
# ---------------------------------------------------------------------------


def test_clone_marketplace_argv_uses_dash_dash_separator(
    fake_git, tmp_path: Path
) -> None:
    """`git clone` argv carries `--` immediately before source.repo.

    Defends against argv flag injection if source.repo ever begins
    with `-` (e.g. `-upload-pack=touch /tmp/pwn`): without `--`, git
    would interpret it as a flag. The list-form argv hygiene already
    prevents shell-level injection; this completes the defense at the
    git-CLI argument-parsing layer.
    """
    from setforge.claude_marketplace_cache import resolve_marketplace_source

    fake = fake_git(known_repos={"anthropic/plug"})
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=tmp_path / "cache"
    )
    clone_calls = [c for c in fake.calls if c[1:2] == ["clone"]]
    assert len(clone_calls) == 1
    argv = clone_calls[0]
    # argv = [git, "clone", "--", clone_url, dest]
    assert argv[1] == "clone"
    assert argv[2] == "--"
    # The bare `anthropic/plug` shorthand is expanded to a full HTTPS URL
    # before cloning — raw `git clone` cannot resolve the shorthand.
    assert argv[3] == "https://github.com/anthropic/plug"


# ---------------------------------------------------------------------------
# _safe_cache_dir (path-traversal guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        ".",
        "..",
        "with/slash",
        "with\\backslash",
        "foo/..",
    ],
)
def test_safe_cache_dir_rejects_traversal_inputs(tmp_path: Path, bad_name: str) -> None:
    """Empty/dot/double-dot/separator inputs raise MarketplaceCacheMiss."""
    from setforge.claude_marketplace_cache import _safe_cache_dir
    from setforge.errors import MarketplaceCacheMiss

    with pytest.raises(MarketplaceCacheMiss):
        _safe_cache_dir(tmp_path, bad_name)


def test_safe_cache_dir_accepts_plain_basename(tmp_path: Path) -> None:
    """A normal basename returns cache_root / name without raising."""
    from setforge.claude_marketplace_cache import _safe_cache_dir

    out = _safe_cache_dir(tmp_path, "plug")
    assert out == tmp_path / "plug"


def testresolve_marketplace_source_rejects_path_traversal_repo(
    fake_git, tmp_path: Path
) -> None:
    """A repo of shape 'foo/..' (basename '..') is rejected pre-clone."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source
    from setforge.errors import MarketplaceCacheMiss

    fake = fake_git(known_repos={"foo/.."})
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="foo/..")
    with pytest.raises(MarketplaceCacheMiss, match="invalid marketplace cache subdir"):
        resolve_marketplace_source(
            src, ClaudeInstallMode.LOCAL_CLONE, cache_root=tmp_path / "cache"
        )
    # And: no rmtree, no clone, no I/O — assert nothing was executed.
    assert fake.clone_count() == 0


def test_sync_marketplace_cache_rejects_path_traversal_repo(
    fake_git, tmp_path: Path
) -> None:
    """sync_marketplace_cache also guards the cache subdir derivation."""
    from setforge.claude_marketplace_cache import sync_marketplace_cache
    from setforge.errors import MarketplaceCacheMiss

    fake_git(known_repos=set())
    cfg = _make_config(
        marketplaces={
            "evil": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="foo/.."
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="evil")},
    )
    profile = _make_resolved(claude_plugins=["a"])
    with pytest.raises(MarketplaceCacheMiss, match="invalid marketplace cache subdir"):
        sync_marketplace_cache(cfg, profile)


# ---------------------------------------------------------------------------
# sync_marketplace_cache semantics
# ---------------------------------------------------------------------------


def test_sync_marketplace_cache_clones_missing(fake_git, tmp_path: Path) -> None:
    """sync_marketplace_cache clones marketplaces absent from the cache."""
    from setforge.claude_marketplace_cache import sync_marketplace_cache

    fake = fake_git(known_repos={"anthropic/plug"})
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug"
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="anthropic")},
    )
    profile = _make_resolved(claude_plugins=["a"])
    refreshed = sync_marketplace_cache(cfg, profile)
    assert refreshed == ["anthropic"]
    assert fake.clone_count() == 1


def test_sync_marketplace_cache_refreshes_existing(fake_git, tmp_path: Path) -> None:
    """sync_marketplace_cache fetch+resets caches that already exist."""
    from setforge.claude_marketplace_cache import sync_marketplace_cache

    fake = fake_git(known_repos={"anthropic/plug"})
    cache_root = tmp_path / "marketplaces"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    fake.cloned[cache_dir] = "anthropic/plug"
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug"
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="anthropic")},
    )
    profile = _make_resolved(claude_plugins=["a"])
    refreshed = sync_marketplace_cache(cfg, profile)
    assert refreshed == ["anthropic"]
    assert fake.clone_count() == 0
    fetch_calls = [c for c in fake.calls if "fetch" in c]
    reset_calls = [c for c in fake.calls if "reset" in c]
    assert fetch_calls
    assert reset_calls


def test_sync_marketplace_cache_honors_legacy_alias_sidecar(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marketplace an earlier version cloned into a non-basename subdir
    (recorded in the alias sidecar) must refresh THAT dir, not the colliding
    basename dir.

    Fails against the old basename-only computation: the basename dir holds
    a different owner's clone, so the origin-mismatch guard raises
    MarketplaceCacheMiss and the aliased marketplace is never refreshable.
    """
    import json

    from setforge import claude_marketplace_cache as mp_cache
    from setforge.claude_marketplace_cache import sync_marketplace_cache

    fake = fake_git(known_repos={"bob/tools", "alice/tools"})
    cache_root = tmp_path / "marketplaces"
    # Colliding basename dir holds a DIFFERENT owner's clone.
    basename_dir = cache_root / "tools"
    basename_dir.mkdir(parents=True)
    (basename_dir / ".git").mkdir()
    fake.cloned[basename_dir] = "alice/tools"
    # bob/tools was cloned into a non-basename subdir + aliased.
    aliased_dir = cache_root / "tools-bob"
    aliased_dir.mkdir(parents=True)
    (aliased_dir / ".git").mkdir()
    fake.cloned[aliased_dir] = "bob/tools"
    (cache_root / ".aliases.json").write_text(json.dumps({"bob/tools": "tools-bob"}))

    refreshed_dirs: list[Path] = []
    real_refresh = mp_cache._refresh_marketplace_cache

    def _spy_refresh(source: object, cache_dir: Path) -> None:
        refreshed_dirs.append(cache_dir)
        real_refresh(source, cache_dir)  # type: ignore[arg-type]

    monkeypatch.setattr(mp_cache, "_refresh_marketplace_cache", _spy_refresh)

    cfg = _make_config(
        marketplaces={
            "bob": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="bob/tools"
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="bob")},
    )
    profile = _make_resolved(claude_plugins=["a"])

    refreshed = sync_marketplace_cache(cfg, profile, cache_root=cache_root)
    assert refreshed == ["bob"]
    assert fake.clone_count() == 0
    # It refreshed the ALIASED dir, never the colliding basename dir.
    assert refreshed_dirs == [aliased_dir]


def test_sync_marketplace_cache_skips_path_sources(fake_git, tmp_path: Path) -> None:
    """PATH-kind marketplaces are skipped (no clone, no fetch)."""
    from setforge.claude_marketplace_cache import sync_marketplace_cache

    fake = fake_git(known_repos=set())
    local = tmp_path / "preinstalled"
    local.mkdir()
    cfg = _make_config(
        marketplaces={
            "local": MarketplaceSource(source=MarketplaceSourceKind.PATH, path=local)
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="local")},
    )
    profile = _make_resolved(claude_plugins=["a"])
    refreshed = sync_marketplace_cache(cfg, profile)
    assert refreshed == []
    assert fake.calls == []


def test_sync_marketplace_cache_no_github_marketplaces_no_op(
    fake_git, tmp_path: Path
) -> None:
    """Empty profile yields empty refresh list, exits cleanly."""
    from setforge.claude_marketplace_cache import sync_marketplace_cache

    fake_git(known_repos=set())
    cfg = _make_config(marketplaces={}, claude_plugins={})
    profile = _make_resolved(claude_plugins=[])
    refreshed = sync_marketplace_cache(cfg, profile)
    assert refreshed == []


def test_sync_marketplace_cache_clone_failure_raises_cache_miss(
    fake_git, tmp_path: Path
) -> None:
    """sync_marketplace_cache surfaces a clone failure as MarketplaceCacheMiss."""
    from setforge.claude_marketplace_cache import sync_marketplace_cache
    from setforge.errors import MarketplaceCacheMiss

    fake_git(known_repos=set())  # any clone fails
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug"
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="anthropic")},
    )
    profile = _make_resolved(claude_plugins=["a"])
    with pytest.raises(MarketplaceCacheMiss, match="sync-cache"):
        sync_marketplace_cache(cfg, profile)


def test_urls_equivalent_is_case_insensitive() -> None:
    """GitHub owner/repo is case-insensitive; a case variant must not read as
    URL-changed (which would raise the cache-collision error every sync)."""
    from setforge.claude_marketplace_cache import _urls_equivalent

    assert _urls_equivalent("https://github.com/Owner/Repo.git", "owner/repo")
    assert _urls_equivalent("owner/repo", "OWNER/REPO")
