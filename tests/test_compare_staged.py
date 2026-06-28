"""compare classifies a reconcile-staged plain file's drift as EXPECTED (A5/A5c).

A staged plain (``disposition: None``) file's tracked holds only the promoted
set, so its live↔tracked diff (host-only LOCAL/PENDING hunks, and the
host-specific side of a SHARED_DRAFTED hunk) is the *expected* staging
divergence — not unsynced drift. A tracked hand-edit that breaks INV-8 still
classifies UNEXPECTED.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from setforge.compare import CompareStatus, DriftClass, compare_profile
from setforge.config import Config, Profile, TrackedFile
from setforge.reconcile import hunks as reconcile_hunks
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.types import HunkClass, file_id

BASE = b"## Worktrees\nUse wt.\n\n## Paths\nworkdir: /home/generic\n"
LIVE = b"## Worktrees\nUse wt.\n\n## Shell\nzsh\n\n## Paths\nworkdir: /home/raul\n"


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _stage(classes: dict[str, HunkClass], drafts: dict[str, bytes] | None = None):
    """Record a reconcile-staged state for file-id 'x' over BASE/LIVE."""
    drafts = drafts or {}
    hunks = []
    for h in reconcile_hunks.extract_hunks(BASE, LIVE):
        cls = classes.get(h.label, HunkClass.PENDING)
        draft_hash = (
            reconcile_store._sha(drafts[h.anchor])
            if cls is HunkClass.SHARED_DRAFTED
            else None
        )
        hunks.append(replace(h, cls=cls, draft_hash=draft_hash))
    reconcile_store.record(
        "p",
        file_id("x"),
        base=BASE,
        local=LIVE,
        hunks=reconcile_hunks.serialize(hunks),
        drafts=drafts,
    )
    return hunks


def _config(tmp_path: Path, tracked: bytes) -> tuple[Config, Path]:
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True, exist_ok=True)
    (repo / "tracked" / "x").write_bytes(tracked)
    dst = tmp_path / "live" / "x"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(LIVE)  # the host's live (with Shell + /home/raul)
    config = Config(
        tracked_files={"x": TrackedFile.model_validate({"src": "x", "dst": str(dst)})},
        profiles={"p": Profile(tracked_files=["x"])},
    )
    return config, repo


def test_staged_shared_local_drift_is_expected(tmp_path: Path) -> None:
    # Shell SHARED (promoted), Paths LOCAL (kept host-only) → tracked omits the
    # host workdir, so tracked != live, but it IS the staged shape.
    hunks = _stage({"## Shell": HunkClass.SHARED, "## Paths": HunkClass.LOCAL})
    tracked = reconcile_hunks.reconstruct(BASE, LIVE, hunks, {})
    config, repo = _config(tmp_path, tracked)

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]
    assert entry.status is CompareStatus.DRIFTED  # live carries the host-only path
    assert entry.drift_class is DriftClass.EXPECTED
    # the CI gate keys on drift_class, not the disposition-intent property:
    # an EXPECTED staged file must not trip `compare --check`.
    assert report.has_unexpected_drift is False


def test_staged_drafted_divergence_is_expected(tmp_path: Path) -> None:
    # Paths SHARED_DRAFTED: tracked has the shareable draft, live keeps host bytes.
    paths = next(
        h for h in reconcile_hunks.extract_hunks(BASE, LIVE) if h.label == "## Paths"
    )
    draft = b"workdir: $HOME\n"
    hunks = _stage({"## Paths": HunkClass.SHARED_DRAFTED}, {paths.anchor: draft})
    tracked = reconcile_hunks.reconstruct(BASE, LIVE, hunks, {paths.anchor: draft})
    config, repo = _config(tmp_path, tracked)

    entry = compare_profile(config, "p", repo).entries[0]
    assert entry.drift_class is DriftClass.EXPECTED  # blessed divergence, no re-flag


def test_tracked_handedit_breaks_inv8_is_unexpected(tmp_path: Path) -> None:
    # tracked carries something the promoted set does not explain → INV-8 fails.
    _stage({"## Shell": HunkClass.SHARED, "## Paths": HunkClass.LOCAL})
    config, repo = _config(tmp_path, BASE + b"\n## Rogue\nhand-edited tracked\n")

    entry = compare_profile(config, "p", repo).entries[0]
    assert entry.drift_class is DriftClass.UNEXPECTED


def test_unstaged_plain_file_is_unexpected(tmp_path: Path) -> None:
    # No reconcile index entry → not this slot's case → ordinary UNEXPECTED drift.
    config, repo = _config(tmp_path, BASE)  # tracked = base, no staging recorded
    entry = compare_profile(config, "p", repo).entries[0]
    assert entry.drift_class is DriftClass.UNEXPECTED
