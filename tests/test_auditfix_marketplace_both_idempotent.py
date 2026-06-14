"""Regression: the ``BOTH`` collision outcome is idempotent across reconciles.

When two GitHub marketplaces share a repo basename under
:data:`ClaudeInstallMode.LOCAL_CLONE`, the collision wizard's ``BOTH``
choice clones the new repo into a wizard-chosen subdir (e.g. ``plug-v2``).
The earlier audit-fix attempt made ``BOTH`` *refuse* (raising
``MarketplaceCacheMiss``), which broke the designed wizard contract. The
correct fix clones into the new subdir AND persists an ``owner/repo ->
subdir`` alias to a cache-root sidecar, so the next reconcile's declared
identity resolves to that subdir instead of recomputing
``cache_root/<basename>`` — making the outcome idempotent (no re-prompt,
no ``MarketplaceCacheMiss`` under ``--auto``).

These tests pin both halves:
- the ``BOTH`` clone persists the sidecar alias, and
- :func:`_source_identity` honors it (and would NOT, pre-fix, under
  basename-only resolution — the explicit pre-fix contrast below).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.claude_marketplace_cache import (
    read_cache_aliases,
    resolve_marketplace_source,
)
from setforge.claude_plugins import _source_identity
from setforge.config import (
    ClaudeInstallMode,
    MarketplaceSource,
    MarketplaceSourceKind,
)
from setforge.marketplace_cache_wizard import CollisionAction, CollisionResolution


def _both_resolution(new_dir: Path):
    return lambda **_: CollisionResolution(
        action=CollisionAction.BOTH, new_cache_dir=new_dir
    )


def test_both_clone_persists_alias_sidecar(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``BOTH`` resolution records ``newowner/plug -> plug-v2`` in the sidecar."""
    fake_git(known_repos={"anthropic/plug", "newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    cache_dir = cache_root / "plug"
    cache_dir.mkdir()
    (cache_dir / ".git").mkdir()
    new_dir = cache_root / "plug-v2"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")
    monkeypatch.setattr(
        "setforge.marketplace_cache_wizard.resolve_collision",
        _both_resolution(new_dir),
    )

    out = resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root, mp_name="anthropic"
    )

    assert out.path == new_dir
    assert read_cache_aliases(cache_root) == {"newowner/plug": "plug-v2"}


def test_subsequent_identity_resolves_to_new_dir_via_sidecar(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a ``BOTH`` clone, the declared identity points at ``plug-v2``.

    This is the idempotency proof: the next reconcile computes the declared
    marketplace's identity via :func:`_source_identity`, and the persisted
    alias must steer it to the new subdir so the marketplace is recognized
    as already-registered (no re-prompt / no cache miss).

    The asserted-against pre-fix value (``cache_root/plug``, the basename
    dir) is the WRONG identity the basename-only computation produced —
    encoding why this test fails without the sidecar.
    """
    fake_git(known_repos={"anthropic/plug", "newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    cache_dir = cache_root / "plug"
    cache_dir.mkdir()
    (cache_dir / ".git").mkdir()
    new_dir = cache_root / "plug-v2"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")
    monkeypatch.setattr(
        "setforge.marketplace_cache_wizard.resolve_collision",
        _both_resolution(new_dir),
    )

    # First reconcile: BOTH clones into plug-v2 and persists the alias.
    resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root, mp_name="anthropic"
    )

    # Next reconcile: the declared identity must resolve to plug-v2 (the
    # registered cache PATH), NOT the basename dir cache_root/plug.
    identity = _source_identity(src, ClaudeInstallMode.LOCAL_CLONE, cache_root)
    assert identity == str(new_dir)
    # Explicit pre-fix contrast: basename-only resolution gave the wrong dir.
    assert identity != str(cache_dir)


def test_identity_without_sidecar_degrades_to_basename(
    fake_git, tmp_path: Path
) -> None:
    """Absent sidecar (legacy / no prior BOTH) keeps plain basename behavior."""
    fake_git(known_repos={"newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")

    identity = _source_identity(src, ClaudeInstallMode.LOCAL_CLONE, cache_root)

    assert identity == str(cache_root / "plug")
