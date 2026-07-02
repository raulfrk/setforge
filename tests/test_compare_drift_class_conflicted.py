"""Tests for structural-file drift classification after the disposition/spans
cutover.

The CONFLICTED drift-class slot was a property of the retired file-level
``disposition: forked`` merge surface: it rendered ``base ≠ live AND
base ≠ tracked`` forked-scalar conflicts. No file carries a disposition or
tracked-side spans anymore, so ``_forked_scalar_conflicts`` always returns
``[]`` and no file classifies CONFLICTED — the whole forked-conflict test
surface is retired.

What remains here are the two scenarios from that surface that map onto
retained, unified-model behavior for a plain structural (YAML) file:

- a byte base present, live == base, tracked advanced → STALE (auto
  fast-forward on the next install; ``forked_scalar_conflicts`` stays empty)
- a byte base present but BOTH live and tracked diverged from it → UNEXPECTED
  (nothing above explains the drift; still empty conflicts)
"""

from pathlib import Path

import pytest

from setforge import base_store
from setforge.compare import (
    CompareStatus,
    DriftClass,
    compare_profile,
)
from setforge.config import Config, Profile, TrackedFile


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_config(tracked_file: TrackedFile) -> Config:
    return Config(
        tracked_files={"x": tracked_file},
        profiles={"p": Profile(tracked_files=["x"])},
    )


_BASE_BODY = "trackedKey: tracked-value\nuserKeyA: placeholder-A\n"
_LIVE_CONFLICT = _BASE_BODY.replace("placeholder-A", "live-edit")
_TRACKED_CONFLICT = _BASE_BODY.replace("placeholder-A", "tracked-edit")


def _structural_file(
    tmp_path: Path,
    *,
    tracked: str,
    live: str,
    suffix: str = ".yaml",
) -> tuple[Config, Path]:
    """One-file plain structural config; returns (config, repo_root)."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / f"x{suffix}", tracked)
    dst = tmp_path / "live" / f"x{suffix}"
    _write(dst, live)
    return _make_config(
        TrackedFile.model_validate({"src": f"x{suffix}", "dst": str(dst)})
    ), repo


def test_tracked_advance_only_classifies_stale(tmp_path: Path) -> None:
    """base == live with tracked advanced: the next install fast-forwards
    live, so the STALE slot wins (no phantom conflict)."""
    config, repo = _structural_file(
        tmp_path, tracked=_TRACKED_CONFLICT, live=_BASE_BODY
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.STALE
    assert entry.forked_scalar_conflicts == []
    assert report.has_unexpected_drift is False


def test_both_sides_diverged_stays_unexpected(tmp_path: Path) -> None:
    """A byte base present with BOTH live and tracked diverged from it is
    plain UNEXPECTED — nothing (stale / reconcile-staged) explains it, and
    no forked-scalar conflict surface exists to reclassify it."""
    config, repo = _structural_file(
        tmp_path, tracked=_TRACKED_CONFLICT, live=_LIVE_CONFLICT
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_class is DriftClass.UNEXPECTED
    assert entry.forked_scalar_conflicts == []
    assert report.has_unexpected_drift is True
