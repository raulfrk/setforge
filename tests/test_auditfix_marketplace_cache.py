"""Regression tests for marketplace-cache audit fixes.

Covers two CONFIRMED findings in
:mod:`setforge.claude_marketplace_cache`:

1. ``sync_marketplace_cache`` refreshed a basename-collision cache dir
   (``alice/tools`` vs ``bob/tools`` both map to ``cache_root/tools``)
   against whatever origin was already there, silently serving wrong
   content. The fix adds an origin-drift check that raises instead.
2. The three git subprocess sites caught only ``CalledProcessError`` /
   ``TimeoutExpired``; an ``OSError`` (git resolved on PATH but failed to
   exec) escaped as a raw traceback. The fix adds ``OSError`` to each
   except tuple so it maps to the module's wrapped error.
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

    with pytest.raises(MarketplaceCacheMiss, match="collide") as raised:
        sync_marketplace_cache(cfg, profile)

    message = str(raised.value)
    assert "marketplace 'bob'" in message
    assert str(cache_dir) in message
    assert "'alice/tools'" in message
    assert "'bob/tools'" in message
    assert f"rm -rf {cache_dir}" in message

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
# Finding 2 — OSError exec failure maps to the wrapped MarketplaceCacheMiss
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
