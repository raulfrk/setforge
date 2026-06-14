"""Regression: LOCAL_CLONE marketplace reconcile must be idempotent.

Audit finding ``localclone_idempotent``: under
:data:`ClaudeInstallMode.LOCAL_CLONE`, a GITHUB marketplace is registered
under its on-disk cache PATH (``marketplace add <cache_path>``). The next
reconcile's declared-vs-registered identity comparison previously reduced
the declared GITHUB source to its ``owner/repo`` slug while the registered
side reported the cache path — they could never compare equal, so the
marketplace was re-added on EVERY reconcile (non-idempotent), contradicting
the module's idempotency promise.

These tests fail on the pre-fix behavior (second reconcile re-adds) and
pass once ``_source_identity`` / ``_marketplaces_to_add`` mirror the cache
path under LOCAL_CLONE.
"""

from pathlib import Path

from setforge import claude_plugins as cp
from setforge.config import (
    ClaudeInstallMode,
    ClaudePluginRef,
    MarketplaceSource,
    MarketplaceSourceKind,
)
from tests.conftest import _local_clone_yaml, _make_config, _make_resolved


def test_local_clone_second_reconcile_does_not_readd_marketplace(
    fake_claude, fake_git, tmp_path, monkeypatch
) -> None:
    """Two reconciles in LOCAL_CLONE add the marketplace exactly once."""
    _local_clone_yaml(tmp_path, monkeypatch)
    fc = fake_claude()
    fake_git(known_repos={"anthropic/plug"})
    cfg = _make_config(
        marketplaces={
            "anthropic": MarketplaceSource(
                source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug"
            )
        },
        claude_plugins={"a": ClaudePluginRef(marketplace="anthropic")},
    )
    profile = _make_resolved(claude_plugins=["a"])

    first = cp.reconcile(cfg, profile)
    assert first.marketplaces_added == ["anthropic"]

    second = cp.reconcile(cfg, profile)
    # The registered cache-path source now matches the declared identity,
    # so the second reconcile is a no-op for marketplaces.
    assert second.marketplaces_added == []
    # marketplace_add ran exactly once across BOTH reconciles.
    assert len(fc.mp_add_args()) == 1


def test_source_identity_local_clone_matches_registered_cache_path() -> None:
    """LOCAL_CLONE declared identity equals the registered cache-path string."""
    cache_root = Path("/tmp/sf-cache")
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    identity = cp._source_identity(src, ClaudeInstallMode.LOCAL_CLONE, cache_root)
    assert identity == str(cache_root / "plug")
    # The path the marketplace gets registered under (cache_root/basename)
    # is exactly what _registered_source_identities would normalize to.
    registered = cp._registered_source_identities(
        {"plug": {"source": str(cache_root / "plug")}}
    )
    assert identity in registered


def test_source_identity_regular_mode_unchanged() -> None:
    """REGULAR mode keeps the owner/repo slug identity (no path swap)."""
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="anthropic/plug")
    assert cp._source_identity(src) == "anthropic/plug"
    assert cp._source_identity(src, ClaudeInstallMode.REGULAR) == "anthropic/plug"
