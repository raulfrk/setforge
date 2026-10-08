"""A cache-root alias sidecar written by an earlier version keeps working.

Earlier versions could clone a repo whose final name collided with another
into a differently named subdir (for example ``plug-v2``) and recorded
``owner/repo -> subdir`` in ``<cache root>/.aliases.json``. This version never
writes that file, but still reads it, so a host that already has one keeps
resolving the marketplace to that subdir: the declared identity points at the
subdir and no cache-collision error is raised.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _host_with_legacy_alias(fake_git, tmp_path: Path) -> tuple[Path, Path, Path]:
    """Cache root holding ``anthropic/plug`` at ``plug`` and ``newowner/plug`` at
    ``plug-v2``, with the sidecar an earlier version would have written."""
    fake = fake_git(known_repos={"anthropic/plug", "newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_dir = cache_root / "plug"
    new_dir = cache_root / "plug-v2"
    for directory in (cache_dir, new_dir):
        directory.mkdir(parents=True)
        (directory / ".git").mkdir()
    fake.cloned[cache_dir] = "anthropic/plug"
    fake.cloned[new_dir] = "newowner/plug"
    (cache_root / ".aliases.json").write_text(
        json.dumps({"newowner/plug": "plug-v2"}), encoding="utf-8"
    )
    return cache_root, cache_dir, new_dir


def test_alias_sidecar_steers_the_colliding_repo_to_its_subdir(
    fake_git, tmp_path: Path
) -> None:
    cache_root, cache_dir, new_dir = _host_with_legacy_alias(fake_git, tmp_path)
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")

    out = resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root, mp_name="anthropic"
    )

    assert out.path == new_dir
    assert read_cache_aliases(cache_root) == {"newowner/plug": "plug-v2"}
    assert cache_dir.exists()


def test_identity_resolves_to_the_aliased_dir_via_sidecar(
    fake_git, tmp_path: Path
) -> None:
    """The declared identity points at ``plug-v2``, the path the marketplace is
    registered under, not at the colliding basename directory."""
    cache_root, cache_dir, new_dir = _host_with_legacy_alias(fake_git, tmp_path)
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")

    identity = _source_identity(src, ClaudeInstallMode.LOCAL_CLONE, cache_root)

    assert identity == str(new_dir)
    assert identity != str(cache_dir)


def test_identity_without_sidecar_degrades_to_basename(
    fake_git, tmp_path: Path
) -> None:
    """Absent sidecar keeps plain basename behavior."""
    fake_git(known_repos={"newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")

    identity = _source_identity(src, ClaudeInstallMode.LOCAL_CLONE, cache_root)

    assert identity == str(cache_root / "plug")
