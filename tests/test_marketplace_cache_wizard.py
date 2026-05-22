"""Tests for :mod:`setforge.marketplace_cache_wizard`.

Covers the four-option (k/u/b/a) collision-resolution surface plus
the spec-locked non-interactive safe-default (auto / non-TTY refuse
to silently auto-pick).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import typer

from setforge.errors import MarketplaceCacheMiss
from setforge.marketplace_cache_wizard import (
    CollisionAction,
    _is_valid_subdir_name,
    resolve_collision,
)


def _make_prompt(responses: list[str]) -> Callable[[str], str]:
    """Return a prompt_fn that pops the next response from ``responses``."""
    queue = list(responses)

    def fn(_msg: str) -> str:
        if not queue:
            raise AssertionError("prompt called more times than responses")
        return queue.pop(0)

    return fn


# --- _is_valid_subdir_name ---


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("plug", True),
        ("plug-2", True),
        ("plug_v2", True),
        ("Plug123", True),
        ("", False),
        (".", False),
        ("..", False),
        ("foo/bar", False),
        ("foo\\bar", False),
        ("foo.bar", False),
        ("foo bar", False),
        ("foo:bar", False),
    ],
)
def test_is_valid_subdir_name(name: str, expected: bool) -> None:
    """The name-validator mirrors the path-traversal guard."""
    assert _is_valid_subdir_name(name) is expected


# --- non-interactive safe defaults ---


def test_resolve_collision_auto_raises_cache_miss(tmp_path: Path) -> None:
    """``auto=True`` refuses to silently auto-pick: raise."""
    with pytest.raises(MarketplaceCacheMiss, match="cache collision"):
        resolve_collision(
            mp_name="anthropic",
            cache_dir=tmp_path / "plug",
            cache_root=tmp_path,
            existing_origin="anthropic/plug",
            new_repo="newowner/plug",
            auto=True,
            stdin_is_tty=lambda: True,  # auto wins over tty
        )


def test_resolve_collision_non_tty_raises_cache_miss(tmp_path: Path) -> None:
    """Non-TTY stdin refuses to silently auto-pick: raise."""
    with pytest.raises(MarketplaceCacheMiss, match="cache collision"):
        resolve_collision(
            mp_name="anthropic",
            cache_dir=tmp_path / "plug",
            cache_root=tmp_path,
            existing_origin="anthropic/plug",
            new_repo="newowner/plug",
            auto=False,
            stdin_is_tty=lambda: False,
        )


# --- interactive branches ---


def test_resolve_collision_keep_returns_keep(tmp_path: Path) -> None:
    """[k] returns KEEP with no new_cache_dir."""
    resolution = resolve_collision(
        mp_name="anthropic",
        cache_dir=tmp_path / "plug",
        cache_root=tmp_path,
        existing_origin="anthropic/plug",
        new_repo="newowner/plug",
        prompt_fn=_make_prompt(["k"]),
        stdin_is_tty=lambda: True,
    )
    assert resolution.action is CollisionAction.KEEP
    assert resolution.new_cache_dir is None


def test_resolve_collision_update_returns_update(tmp_path: Path) -> None:
    """[u] returns UPDATE with no new_cache_dir."""
    resolution = resolve_collision(
        mp_name="anthropic",
        cache_dir=tmp_path / "plug",
        cache_root=tmp_path,
        existing_origin="anthropic/plug",
        new_repo="newowner/plug",
        prompt_fn=_make_prompt(["u"]),
        stdin_is_tty=lambda: True,
    )
    assert resolution.action is CollisionAction.UPDATE


def test_resolve_collision_abort_raises_typer_abort(tmp_path: Path) -> None:
    """[a] raises typer.Abort."""
    with pytest.raises(typer.Abort):
        resolve_collision(
            mp_name="anthropic",
            cache_dir=tmp_path / "plug",
            cache_root=tmp_path,
            existing_origin="anthropic/plug",
            new_repo="newowner/plug",
            prompt_fn=_make_prompt(["a"]),
            stdin_is_tty=lambda: True,
        )


def test_resolve_collision_both_with_valid_name(tmp_path: Path) -> None:
    """[b] with a valid name returns BOTH carrying the new cache_dir."""
    resolution = resolve_collision(
        mp_name="anthropic",
        cache_dir=tmp_path / "plug",
        cache_root=tmp_path,
        existing_origin="anthropic/plug",
        new_repo="newowner/plug",
        prompt_fn=_make_prompt(["b"]),
        name_prompt_fn=_make_prompt(["plug-v2"]),
        stdin_is_tty=lambda: True,
    )
    assert resolution.action is CollisionAction.BOTH
    assert resolution.new_cache_dir == tmp_path / "plug-v2"


def test_resolve_collision_both_rejects_traversal_name_and_retries(
    tmp_path: Path,
) -> None:
    """[b] re-prompts on invalid name; accepts on second try."""
    resolution = resolve_collision(
        mp_name="anthropic",
        cache_dir=tmp_path / "plug",
        cache_root=tmp_path,
        existing_origin="anthropic/plug",
        new_repo="newowner/plug",
        prompt_fn=_make_prompt(["b"]),
        name_prompt_fn=_make_prompt(["..", "plug-v2"]),
        stdin_is_tty=lambda: True,
    )
    assert resolution.action is CollisionAction.BOTH
    assert resolution.new_cache_dir == tmp_path / "plug-v2"


def test_resolve_collision_both_rejects_existing_name(tmp_path: Path) -> None:
    """[b] re-prompts when the chosen name already exists in cache_root."""
    (tmp_path / "plug-v2").mkdir()
    resolution = resolve_collision(
        mp_name="anthropic",
        cache_dir=tmp_path / "plug",
        cache_root=tmp_path,
        existing_origin="anthropic/plug",
        new_repo="newowner/plug",
        prompt_fn=_make_prompt(["b"]),
        name_prompt_fn=_make_prompt(["plug-v2", "plug-v3"]),
        stdin_is_tty=lambda: True,
    )
    assert resolution.new_cache_dir == tmp_path / "plug-v3"


def test_resolve_collision_both_too_many_invalid_names_raises(
    tmp_path: Path,
) -> None:
    """[b] gives up after 3 invalid names and raises MarketplaceCacheMiss."""
    with pytest.raises(MarketplaceCacheMiss, match="too many invalid"):
        resolve_collision(
            mp_name="anthropic",
            cache_dir=tmp_path / "plug",
            cache_root=tmp_path,
            existing_origin="anthropic/plug",
            new_repo="newowner/plug",
            prompt_fn=_make_prompt(["b"]),
            name_prompt_fn=_make_prompt(["..", "/etc", "foo/bar"]),
            stdin_is_tty=lambda: True,
        )


def test_resolve_collision_invalid_action_re_prompts(tmp_path: Path) -> None:
    """An unknown action key re-prompts until a valid one arrives."""
    resolution = resolve_collision(
        mp_name="anthropic",
        cache_dir=tmp_path / "plug",
        cache_root=tmp_path,
        existing_origin="anthropic/plug",
        new_repo="newowner/plug",
        prompt_fn=_make_prompt(["x", "zzz", "k"]),
        stdin_is_tty=lambda: True,
    )
    assert resolution.action is CollisionAction.KEEP


def test_resolve_collision_keep_accepts_full_word(tmp_path: Path) -> None:
    """Full-word forms (``keep`` / ``update`` / ``both`` / ``abort``) work."""
    resolution = resolve_collision(
        mp_name="anthropic",
        cache_dir=tmp_path / "plug",
        cache_root=tmp_path,
        existing_origin="anthropic/plug",
        new_repo="newowner/plug",
        prompt_fn=_make_prompt(["KEEP"]),
        stdin_is_tty=lambda: True,
    )
    assert resolution.action is CollisionAction.KEEP


# --- integration: claude_plugins drives the wizard ---


def testresolve_marketplace_source_url_drift_keep_uses_existing_cache(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[k]eep returns the existing cache_dir and runs zero clones."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source
    from setforge.config import (
        ClaudeInstallMode,
        MarketplaceSource,
        MarketplaceSourceKind,
    )

    fake = fake_git(known_repos={"anthropic/plug", "newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    fake.cloned[cache_dir] = "anthropic/plug"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")

    from setforge.marketplace_cache_wizard import (
        CollisionAction,
        CollisionResolution,
    )

    monkeypatch.setattr(
        "setforge.marketplace_cache_wizard.resolve_collision",
        lambda **_: CollisionResolution(action=CollisionAction.KEEP),
    )
    out = resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root, mp_name="anthropic"
    )
    assert out.source is MarketplaceSourceKind.PATH
    assert out.path == cache_dir
    assert fake.clone_count() == 0
    # Existing cache content untouched.
    assert cache_dir.exists()


def testresolve_marketplace_source_url_drift_both_clones_into_new_subdir(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[b]oth clones into a fresh subdir; existing cache stays put."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source
    from setforge.config import (
        ClaudeInstallMode,
        MarketplaceSource,
        MarketplaceSourceKind,
    )
    from setforge.marketplace_cache_wizard import (
        CollisionAction,
        CollisionResolution,
    )

    fake = fake_git(known_repos={"anthropic/plug", "newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    cache_dir = cache_root / "plug"
    cache_dir.mkdir()
    (cache_dir / ".git").mkdir()
    fake.cloned[cache_dir] = "anthropic/plug"
    new_dir = cache_root / "plug-v2"
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")
    monkeypatch.setattr(
        "setforge.marketplace_cache_wizard.resolve_collision",
        lambda **_: CollisionResolution(
            action=CollisionAction.BOTH, new_cache_dir=new_dir
        ),
    )
    out = resolve_marketplace_source(
        src, ClaudeInstallMode.LOCAL_CLONE, cache_root=cache_root, mp_name="anthropic"
    )
    assert out.path == new_dir
    assert fake.clone_count() == 1
    # Existing cache untouched.
    assert cache_dir.exists()
    assert new_dir.exists()


def testresolve_marketplace_source_url_drift_abort_propagates(
    fake_git, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """typer.Abort from the wizard propagates out of resolve_marketplace_source."""
    from setforge.claude_marketplace_cache import resolve_marketplace_source
    from setforge.config import (
        ClaudeInstallMode,
        MarketplaceSource,
        MarketplaceSourceKind,
    )

    fake_git(known_repos={"anthropic/plug", "newowner/plug"})
    cache_root = tmp_path / "cache"
    cache_dir = cache_root / "plug"
    cache_dir.mkdir(parents=True)
    (cache_dir / ".git").mkdir()
    src = MarketplaceSource(source=MarketplaceSourceKind.GITHUB, repo="newowner/plug")

    def _raise_abort(**_: object) -> None:
        raise typer.Abort()

    monkeypatch.setattr(
        "setforge.marketplace_cache_wizard.resolve_collision",
        _raise_abort,
    )
    with pytest.raises(typer.Abort):
        resolve_marketplace_source(
            src,
            ClaudeInstallMode.LOCAL_CLONE,
            cache_root=cache_root,
            mp_name="anthropic",
        )
