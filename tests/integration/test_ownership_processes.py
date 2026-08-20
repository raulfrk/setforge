"""Fresh-process ownership and target-lock integration boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from setforge.errors import SetforgeError
from setforge.locking import TargetLockRequest, mutation_locks, target_locks
from setforge.ownership import (
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
    ResourceScope,
    ScopeKind,
)


def _claim_worker(store_root: str, home: str, owner: str) -> str:
    os.environ["HOME"] = home
    store = OwnershipStore(Path(store_root))
    resource = ResourceId(
        "package",
        "cargo",
        "ripgrep",
        ResourceScope(ScopeKind.USER_HOST, "current-user"),
    )
    try:
        with mutation_locks(resources=True, timeout=5):
            claim = store.claim_locked(
                resource_id=resource,
                owner_id=uuid.UUID(owner),
                declaration_refs=(f"owner:{owner}",),
                provenance=(ProvenanceFact(ProvenanceFactKind.ORIGIN, "external"),),
                locator="~/.cargo/bin/rg",
                fingerprint="sha256:abc",
                expected_generation=None,
            )
    except SetforgeError as exc:
        return f"refused:{type(exc).__name__}"
    return f"claimed:{claim.owner_id}"


_HOLDER = """
import sys
from pathlib import Path
from setforge.locking import TargetLockRequest, target_locks

target = Path(sys.argv[1])
ready = Path(sys.argv[2])
release = Path(sys.argv[3])
create = sys.argv[4] == "create"
with target_locks((TargetLockRequest(target),), timeout=5) as guards:
    if create:
        guards[0].mkdir()
    ready.write_text("ready", encoding="ascii")
    while not release.exists():
        release.parent.stat()
"""


def _wait_for(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        if process.poll() is not None:
            raise AssertionError(f"lock holder exited early: {process.returncode}")
        if time.monotonic() >= deadline:
            raise AssertionError("lock holder did not become ready")
        time.sleep(0.01)


def test_two_process_claim_collision_has_one_owner(tmp_path: Path) -> None:
    store_root = tmp_path / "ownership"
    home = tmp_path / "home"
    first = uuid.uuid4()
    second = uuid.uuid4()

    with ProcessPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                _claim_worker,
                (str(store_root), str(store_root)),
                (str(home), str(home)),
                (str(first), str(second)),
            )
        )

    assert sum(result.startswith("claimed:") for result in results) == 1
    assert sum(result == "refused:OwnershipCollisionError" for result in results) == 1
    claim = OwnershipStore(store_root).list_claims()[0]
    assert str(claim.owner_id) in {str(first), str(second)}


def test_missing_target_creation_keeps_one_lock_namespace(tmp_path: Path) -> None:
    parent = tmp_path / "projects"
    parent.mkdir()
    target = parent / "demo"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER,
            str(target),
            str(ready),
            str(release),
            "create",
        ],
        cwd=Path(__file__).parents[2],
    )
    try:
        _wait_for(ready, process)
        assert target.is_dir()
        with (
            pytest.raises(SetforgeError, match="target lock"),
            target_locks((TargetLockRequest(target),), timeout=0.1),
        ):
            pass
    finally:
        release.write_text("release", encoding="ascii")
        assert process.wait(timeout=10) == 0


def test_missing_target_creation_blocks_post_creation_alias(tmp_path: Path) -> None:
    parent = tmp_path / "projects"
    parent.mkdir()
    target = parent / "demo"
    alias = tmp_path / "alias"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER,
            str(target),
            str(ready),
            str(release),
            "create",
        ],
        cwd=Path(__file__).parents[2],
    )
    try:
        _wait_for(ready, process)
        alias.symlink_to(target, target_is_directory=True)
        with (
            pytest.raises(SetforgeError, match="target lock"),
            target_locks((TargetLockRequest(alias),), timeout=0.1),
        ):
            pass
    finally:
        release.write_text("release", encoding="ascii")
        assert process.wait(timeout=10) == 0


def test_symlink_alias_contends_on_object_lock(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(target), str(ready), str(release), "keep"],
        cwd=Path(__file__).parents[2],
    )
    try:
        _wait_for(ready, process)
        with (
            pytest.raises(SetforgeError, match="target lock"),
            target_locks((TargetLockRequest(alias),), timeout=0.1),
        ):
            pass
    finally:
        release.write_text("release", encoding="ascii")
        assert process.wait(timeout=10) == 0
