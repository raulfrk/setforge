"""Tests for marketplace + git + cache plumbing (``setforge.claude_marketplace_cache``).

Exercises ``resolve_marketplace_source`` (install-mode dispatch),
``_clone_marketplace`` argv hygiene, ``_safe_cache_dir`` path-traversal
guards, and ``sync_marketplace_cache`` semantics. The ``fake_git``
fixture (defined in :mod:`tests.conftest`) wires :class:`FakeGit` into
the new module's ``subprocess`` / ``shutil`` namespace so
monkeypatch paths track the split.
"""

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


def testresolve_marketplace_source_url_drift_invokes_wizard(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache-origin URL drift dispatches the wizard; [u]pdate re-clones."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source
    from setforge.marketplace_cache_wizard import (
        CollisionAction,
        CollisionResolution,
    )

    fake = fake_git(known_repos={"anthropic/plug", "newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    # Pre-populate cache with a stale origin URL.
    fake.cloned[cache_dir] = "anthropic/plug"
    # Source now declares a different owner.
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")
    # Wizard returns UPDATE — the pre-wizard silent behavior.
    monkeypatch.setattr(
        "setforge.marketplace_cache_wizard.resolve_collision",
        lambda **_: CollisionResolution(action=CollisionAction.UPDATE),
    )
    resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root
    )
    assert fake.clone_count() == 1


def testresolve_marketplace_source_url_drift_non_tty_no_auto_raises(
    fake_git, tmp_path: Path
) -> None:
    """Cache collision under non-TTY + no --auto raises MarketplaceCacheMiss."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source
    from setforge.errors import MarketplaceCacheMiss

    fake = fake_git(known_repos={"anthropic/plug", "newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    fake.cloned[cache_dir] = "anthropic/plug"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")
    # pytest has no TTY → wizard refuses to silently auto-pick.
    with pytest.raises(MarketplaceCacheMiss, match="cache collision"):
        resolve_marketplace_source(
            src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root
        )
    # No destructive action — existing cache untouched, no clone.
    assert cache_dir.exists()
    assert fake.clone_count() == 0


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
    # argv = [git, "clone", "--", repo, dest]
    assert argv[1] == "clone"
    assert argv[2] == "--"
    assert argv[3] == "anthropic/plug"


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
