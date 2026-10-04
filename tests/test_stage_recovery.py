"""A failed ``stage`` rolls back while it still holds its mutation locks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from setforge import locking, operations
from setforge.cli import stage as stage_mod
from setforge.cli.stage import (
    Decision,
    collect_stages,
    collect_structured_stages,
    walk,
    walk_structured,
)
from setforge.config import resolve_profile
from setforge.ownership import OwnershipStore
from setforge.reconcile import store
from setforge.reconcile.types import HunkClass
from tests.test_stage import _setup
from tests.test_stage_structured import _setup_structured


def _line_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    (stage,) = collect_stages(cfg, resolve_profile(cfg, profile), repo, profile)
    result = walk(stage.hunks, lambda h, i, n: Decision(HunkClass.SHARED))
    return lambda: stage_mod._apply(profile, stage, result, owner_id=uuid4())


def _structured_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], None]:
    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    (stage,) = collect_structured_stages(
        cfg, resolve_profile(cfg, profile), repo, profile
    )
    result = walk_structured(stage.units, lambda u, i, t: Decision(HunkClass.LOCAL))
    return lambda: stage_mod._apply_structured(profile, stage, result, owner_id=uuid4())


def _fail_after_record(monkeypatch: pytest.MonkeyPatch) -> None:
    real_commit = stage_mod._commit_persist

    def fail_after_record(*args: object, **kwargs: object) -> None:
        real_commit(*args, **kwargs)  # type: ignore[arg-type]
        raise OSError("injected post-record failure")

    monkeypatch.setattr(stage_mod, "_commit_persist", fail_after_record)


@pytest.mark.parametrize("prepare_stage", [_line_stage, _structured_stage])
def test_failed_stage_recovers_under_its_mutation_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare_stage: Callable[[Path, pytest.MonkeyPatch], Callable[[], None]],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    apply = prepare_stage(tmp_path, monkeypatch)
    held_during_recovery: list[set[locking.LockRank]] = []
    real_recover = operations.recover_automatically

    def recording_recover(journal: operations.OperationJournal) -> bool:
        held_during_recovery.append({rank for rank, _key in locking._HELD_RANKS.get()})
        return real_recover(journal)

    monkeypatch.setattr(operations, "recover_automatically", recording_recover)
    _fail_after_record(monkeypatch)

    with pytest.raises(OSError, match="injected post-record failure"):
        apply()

    (held,) = held_during_recovery
    assert {
        locking.LockRank.MUTATION,
        locking.LockRank.RESOURCES,
        locking.LockRank.PROFILE,
    } <= held
    assert operations.active("p") is None


def test_failed_stage_recovers_when_an_ownership_intents_directory_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    apply = _line_stage(tmp_path, monkeypatch)
    ownership = OwnershipStore()
    ownership.intents_root.mkdir(parents=True)
    index_before = store._index_path("p").read_bytes()
    _fail_after_record(monkeypatch)

    with pytest.raises(OSError, match="injected post-record failure") as raised:
        apply()

    assert not getattr(raised.value, "__notes__", [])
    assert operations.active("p") is None
    assert not tuple(ownership.claims_root.glob("*.json"))
    assert store._index_path("p").read_bytes() == index_before
