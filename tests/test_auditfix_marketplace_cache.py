"""Regression tests for marketplace-cache audit fixes.

Covers two CONFIRMED findings in
:mod:`setforge.claude_marketplace_cache`:

1. ``sync_marketplace_cache`` refreshed a basename-collision cache dir
   (``alice/tools`` vs ``bob/tools`` both map to ``cache_root/tools``)
   against whatever origin was already there, silently serving wrong
   content. The fix adds an origin-drift check that raises instead.
2. ``_collision_update`` rmtree'd the existing cache *before* re-cloning,
   so a failed clone (offline) destroyed the only local copy. The fix
   stages the clone in a sibling temp dir and only swaps on success.
3. The three git subprocess sites caught only ``CalledProcessError`` /
   ``TimeoutExpired``; an ``OSError`` (git resolved on PATH but failed to
   exec) escaped as a raw traceback. The fix adds ``OSError`` to each
   except tuple so it maps to the module's wrapped error.
4. The ``BOTH`` collision outcome cloned into a wizard-chosen ``new_dir``
   that no layer persisted, so under ``LOCAL_CLONE`` the next reconcile
   recomputed the declared identity as ``cache_root/<basename>`` and
   re-fired the wizard every install (non-idempotent). The fix refuses
   ``BOTH`` with a rename-the-basename remediation, the same one
   ``sync_marketplace_cache`` already gives for a colliding basename.
"""

import subprocess
from pathlib import Path

import pytest

from setforge.config import (
    ClaudePluginRef,
    MarketplaceSource,
    MarketplaceSourceKind,
)
from tests.conftest import _make_config, _make_resolved

# ---------------------------------------------------------------------------
# Finding 1 — basename collision in sync_marketplace_cache
# ---------------------------------------------------------------------------


def test_sync_basename_collision_refuses_wrong_origin(fake_git, tmp_path: Path) -> None:
    """A cache dir holding a different owner's repo is NOT refreshed.

    ``bob/tools`` and ``alice/tools`` share the basename ``tools`` and
    map to the same ``cache_root/tools``. With alice's clone already
    present, syncing the bob marketplace must refuse rather than
    fetch+reset bob's declared repo against alice's checkout.
    """
    from setforge.claude_marketplace_cache import sync_marketplace_cache
    from setforge.errors import MarketplaceCacheMiss

    fake = fake_git(known_repos={"alice/tools", "bob/tools"})
    cache_root = tmp_path / "marketplaces"
    cache_dir = cache_root / "tools"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    # The existing clone's origin is alice/tools.
    fake.cloned[cache_dir] = "alice/tools"

    cfg = _make_config(
        marketplaces={
            "bob": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="bob/tools"
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="bob")},
    )
    profile = _make_resolved(claude_plugins=["a"])

    with pytest.raises(MarketplaceCacheMiss, match="collide"):
        sync_marketplace_cache(cfg, profile)

    # The wrong-origin cache was left untouched: no fetch, no reset.
    assert not any("fetch" in c for c in fake.calls)
    assert not any("reset" in c for c in fake.calls)


def test_sync_matching_origin_still_refreshes(fake_git, tmp_path: Path) -> None:
    """A cache whose origin matches the declared repo still fetch+resets.

    Guards the new drift check against over-rejecting: the equivalence
    normalization must accept the same repo and proceed to refresh.
    """
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
    assert any("fetch" in c for c in fake.calls)
    assert any("reset" in c for c in fake.calls)


# ---------------------------------------------------------------------------
# Finding 2 — _collision_update must not destroy the cache on clone failure
# ---------------------------------------------------------------------------


def test_collision_update_clone_failure_preserves_cache(
    fake_git, tmp_path: Path
) -> None:
    """A failed re-clone leaves the existing cache and its content intact.

    ``_collision_update`` is the ``UPDATE`` collision action. When the
    re-clone fails (offline), the original cache_dir — the offline
    source of truth in LOCAL_CLONE mode — must survive untouched, and no
    leftover staging dir may remain.
    """
    from setforge.claude_marketplace_cache import _collision_update
    from setforge.errors import MarketplaceCacheMiss

    # Empty known_repos => every clone fails with CalledProcessError.
    fake_git(known_repos=set())
    cache_root = tmp_path / "marketplaces"
    cache_dir = cache_root / "tools"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    sentinel = cache_dir / "marketplace.json"
    sentinel.write_text('{"owner": "alice"}', encoding="utf-8")

    source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="bob/tools")

    with pytest.raises(MarketplaceCacheMiss):
        _collision_update(source, cache_dir)

    # Original cache + its content survive; no destroyed fallback.
    assert cache_dir.exists()
    assert sentinel.exists()
    assert sentinel.read_text(encoding="utf-8") == '{"owner": "alice"}'
    # No leftover staging sibling.
    assert not (cache_root / "tools.tmp").exists()


def test_collision_update_success_swaps_in_new_clone(fake_git, tmp_path: Path) -> None:
    """A successful re-clone replaces the cache and clears the staging dir."""
    from setforge.claude_marketplace_cache import _collision_update

    fake = fake_git(known_repos={"bob/tools"})
    cache_root = tmp_path / "marketplaces"
    cache_dir = cache_root / "tools"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    (cache_dir / "stale.txt").write_text("old", encoding="utf-8")

    source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="bob/tools")

    out = _collision_update(source, cache_dir)

    assert out.source is MarketplaceSourceKind.PATH
    assert out.path == cache_dir
    assert cache_dir.exists()
    # Stale content was replaced by the fresh clone (no stale.txt).
    assert not (cache_dir / "stale.txt").exists()
    assert (cache_dir / ".git").exists()
    # Staging dir cleaned up after the rename.
    assert not (cache_root / "tools.tmp").exists()
    # The clone was directed at bob/tools.
    clone_calls = [c for c in fake.calls if c[1:2] == ["clone"]]
    assert clone_calls
    assert "bob/tools" in clone_calls[0]


def test_collision_update_clears_stale_staging_dir(fake_git, tmp_path: Path) -> None:
    """A leftover ``.tmp`` staging dir from a prior run is cleared first.

    Without the pre-clean, cloning into a non-empty dest would fail.
    """
    from setforge.claude_marketplace_cache import _collision_update

    fake_git(known_repos={"bob/tools"})
    cache_root = tmp_path / "marketplaces"
    cache_dir = cache_root / "tools"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    # Simulate an interrupted prior UPDATE: a stale staging dir survives.
    staging = cache_root / "tools.tmp"
    staging.mkdir()
    (staging / "junk").write_text("partial", encoding="utf-8")

    source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="bob/tools")

    out = _collision_update(source, cache_dir)
    assert out.path == cache_dir
    assert not staging.exists()


# ---------------------------------------------------------------------------
# Finding 3 — OSError exec failure maps to the wrapped MarketplaceCacheMiss
# ---------------------------------------------------------------------------


def _wire_git_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``git`` resolvable on PATH but raise ``OSError`` on exec.

    Models the gap the fix closes: ``shutil.which('git')`` succeeds, so
    the on-PATH guard passes, but ``subprocess.run`` itself raises
    ``OSError`` (e.g. ``ETXTBSY`` / corrupt binary). Pre-fix this escaped
    as a raw traceback; post-fix it maps to ``MarketplaceCacheMiss``.
    """
    monkeypatch.setattr(
        "setforge.claude_marketplace_cache.shutil.which",
        lambda name: "/usr/bin/git" if name == "git" else None,
    )

    def _raise_oserror(*_args: object, **_kwargs: object) -> object:
        raise OSError("exec format error")

    monkeypatch.setattr(
        "setforge.claude_marketplace_cache.subprocess.run", _raise_oserror
    )


def test_run_git_maps_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``_run_git`` (fetch/reset site) wraps an exec ``OSError``."""
    from setforge.claude_marketplace_cache import _run_git
    from setforge.errors import MarketplaceCacheMiss

    _wire_git_oserror(monkeypatch)
    with pytest.raises(MarketplaceCacheMiss):
        _run_git("fetch", "origin", cwd=tmp_path)


def test_clone_marketplace_maps_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_clone_marketplace`` wraps an exec ``OSError`` from git clone."""
    from setforge.claude_marketplace_cache import _clone_marketplace
    from setforge.errors import MarketplaceCacheMiss

    _wire_git_oserror(monkeypatch)
    source = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="a/b")
    with pytest.raises(MarketplaceCacheMiss):
        _clone_marketplace(source, tmp_path / "dest")


def test_cache_origin_url_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_cache_origin_url`` degrades to ``None`` on an exec ``OSError``.

    The silent-probe site must not raise: an exec failure leaves the
    origin unknown, and the caller falls through to a re-clone.
    """
    from setforge.claude_marketplace_cache import _cache_origin_url

    _wire_git_oserror(monkeypatch)
    assert _cache_origin_url(tmp_path) is None


def test_oserror_is_not_called_process_error() -> None:
    """Guard: OSError is distinct from the already-handled subprocess errors.

    Documents why the except tuple needed a third member — OSError is not
    a subclass of CalledProcessError or TimeoutExpired.
    """
    assert not issubclass(OSError, subprocess.SubprocessError)
