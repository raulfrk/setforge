"""Tests for the retired forked-scalar base store's guarded manifest path."""

from pathlib import Path

import pytest

from setforge import scalar_base_store
from setforge.errors import BaseStoreError


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path))
    return tmp_path


def test_scalar_base_root_is_sibling_of_base(state_dir: Path) -> None:
    assert scalar_base_store.scalar_base_root() == state_dir / "scalar-base"


def test_manifest_path_per_profile_and_file(state_dir: Path) -> None:
    assert scalar_base_store.manifest_path("vm", "settings") == (
        state_dir.resolve() / "scalar-base" / "vm" / "settings.json"
    )
    assert scalar_base_store.manifest_path("vm", "claude/settings") == (
        state_dir.resolve() / "scalar-base" / "vm" / "claude" / "settings.json"
    )


def test_manifest_path_rejects_traversal() -> None:
    with pytest.raises(BaseStoreError, match=r"^unsafe file-id '\.\./escape'"):
        scalar_base_store.manifest_path("vm", "../escape")
    with pytest.raises(BaseStoreError, match=r"^unsafe file-id 'a/\.\./\.\./b'"):
        scalar_base_store.manifest_path("vm", "a/../../b")


def test_manifest_path_rejects_absolute() -> None:
    with pytest.raises(BaseStoreError, match=r"^unsafe file-id '/etc/passwd'"):
        scalar_base_store.manifest_path("vm", "/etc/passwd")


def test_manifest_path_rejects_symlink_escape(state_dir: Path) -> None:
    profile_root = state_dir / "scalar-base" / "vm"
    profile_root.mkdir(parents=True)
    (profile_root / "out").symlink_to(state_dir)
    with pytest.raises(
        BaseStoreError,
        match=r"^file-id 'out/x' resolves outside scalar-base/vm/$",
    ):
        scalar_base_store.manifest_path("vm", "out/x")
