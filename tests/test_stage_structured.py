"""Tests for the structured (YAML/JSON/JSONC) key-level `setforge stage` path."""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.cli.stage import (
    Decision,
    _apply_structured,
    collect_stages,
    collect_structured_stages,
    walk_structured,
)
from setforge.config import Config, Profile, TrackedFile, resolve_profile
from setforge.reconcile.structured_units import StructuredFormat
from setforge.reconcile.types import HunkClass, file_id


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _setup_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Config, Path, str]:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store

    base = b"theme: dark\nfontSize: 14\n"
    live = b"theme: dark\nfontSize: 16\n"
    repo = tmp_path / "repo"
    src = repo / "tracked" / "settings.yaml"
    dst = tmp_path / "live" / "settings.yaml"
    _write(src, base)
    _write(dst, live)
    with locking.profile_lock("p"):
        store.record("p", file_id("settings.yaml"), base=base, local=live)
    cfg = Config(
        tracked_files={
            "settings.yaml": TrackedFile(src=Path("settings.yaml"), dst=str(dst))
        },
        profiles={"p": Profile(tracked_files=["settings.yaml"])},
    )
    return cfg, repo, "p"


def test_collect_structured_yields_key_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A structured tracked file produces per-KEY units (PENDING), not line hunks."""
    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)

    (stage,) = collect_structured_stages(cfg, resolved, repo, profile)

    assert stage.fmt is StructuredFormat.YAML
    assert [u.path for u in stage.units] == ["fontSize"]
    assert all(u.cls is HunkClass.PENDING for u in stage.units)


def test_collect_stages_skips_structured_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The line-hunk collect delegates structured files to the structured path."""
    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)

    # a .yaml must NOT be line-hunk-staged (else it'd be diffed by line, not key)
    assert collect_stages(cfg, resolved, repo, profile) == []


def test_persist_structured_records_key_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classifying a key LOCAL persists a kind:'key' row with cls=local."""
    from setforge.reconcile import store

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_structured_stages(cfg, resolved, repo, profile)

    result = walk_structured(stage.units, lambda u, i, t: Decision(cls=HunkClass.LOCAL))
    _apply_structured(profile, stage, result)

    entry = store.read_index(profile).files[str(file_id("settings.yaml"))]
    rows = {r["path"]: r["cls"] for r in entry.hunks}
    assert rows == {"fontSize": "local"}


def test_walk_structured_shared_records_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classifying a key SHARED persists cls=shared (capture promotes on sync)."""
    from setforge.reconcile import store

    cfg, repo, profile = _setup_structured(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_structured_stages(cfg, resolved, repo, profile)

    result = walk_structured(
        stage.units, lambda u, i, t: Decision(cls=HunkClass.SHARED)
    )
    _apply_structured(profile, stage, result)

    entry = store.read_index(profile).files[str(file_id("settings.yaml"))]
    assert {r["path"]: r["cls"] for r in entry.hunks} == {"fontSize": "shared"}
