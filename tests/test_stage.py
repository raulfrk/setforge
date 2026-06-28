"""Tests for the A5 `setforge stage` command core (collect / walk / persist)."""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge.cli.stage import QUIT, _Quit, collect_stages, counts, walk
from setforge.config import Config, Profile, TrackedFile, resolve_profile
from setforge.reconcile.types import HunkClass, file_id

_BASE = b"## Tool prefs\nUse rg not grep.\n\n## Host paths\nworkdir: /home/generic\n"
_LIVE = (
    b"## Tool prefs\nUse rg not grep.\n\n"
    b"## Shell\nPrefer zsh.\n\n"
    b"## Host paths\nworkdir: /home/raul\n"
)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path, str]:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge import locking
    from setforge.reconcile import store

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, _BASE)
    _write(dst, _LIVE)
    # install-style: record the merge base + empty hunks for the file.
    with locking.profile_lock("p"):
        store.record("p", file_id("CLAUDE.md"), base=_BASE, local=_LIVE)
    cfg = Config(
        tracked_files={"CLAUDE.md": TrackedFile(src=Path("CLAUDE.md"), dst=str(dst))},
        profiles={"p": Profile(tracked_files=["CLAUDE.md"])},
    )
    return cfg, repo, "p"


def test_collect_classifies_unstaged_as_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    assert {h.label for h in stage.hunks} == {"## Shell", "## Host paths"}
    assert all(h.cls is HunkClass.PENDING for h in stage.hunks)


def test_collect_skips_file_with_no_recorded_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    dst = tmp_path / "live" / "x"
    _write(dst, b"local only\n")
    cfg = Config(
        tracked_files={"x": TrackedFile(src=Path("x"), dst=str(dst))},
        profiles={"p": Profile(tracked_files=["x"])},
    )
    resolved = resolve_profile(cfg, "p")
    assert collect_stages(cfg, resolved, repo, "p") == []  # no base → not eligible


def test_collect_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    from setforge.reconcile import store

    resolved = resolve_profile(cfg, profile)
    index_path = store._index_path(profile)
    before = index_path.stat().st_mtime_ns
    collect_stages(cfg, resolved, repo, profile)  # the `--list` data path
    assert index_path.stat().st_mtime_ns == before  # wrote nothing


def test_walk_applies_choices_and_quits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)

    # Share the first hunk, then quit before the second.
    decisions: list[HunkClass | None | _Quit] = [HunkClass.SHARED, QUIT]
    scripted = iter(decisions)
    updated = walk(stage.hunks, lambda h, i, n: next(scripted))

    assert updated[0].cls is HunkClass.SHARED
    assert updated[1].cls is HunkClass.PENDING  # untouched (quit before it)


def test_walk_skip_leaves_class_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    updated = walk(stage.hunks, lambda h, i, n: None)  # skip every hunk
    assert all(h.cls is HunkClass.PENDING for h in updated)


def test_persist_writes_classes_and_keeps_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, repo, profile = _setup(tmp_path, monkeypatch)
    from setforge.cli.stage import _persist
    from setforge.reconcile import store

    resolved = resolve_profile(cfg, profile)
    (stage,) = collect_stages(cfg, resolved, repo, profile)
    updated = walk(
        stage.hunks,
        lambda h, i, n: HunkClass.SHARED if h.label == "## Shell" else HunkClass.LOCAL,
    )
    _persist(profile, stage, updated)

    entry = store.read_index(profile).files["CLAUDE.md"]
    classes = {row["label"]: row["cls"] for row in entry.hunks}
    assert classes == {"## Shell": "shared", "## Host paths": "local"}
    assert store.read_base(profile, file_id("CLAUDE.md")) == _BASE  # base unchanged
    assert store.reconstruct(profile, file_id("CLAUDE.md")) == _LIVE  # full live


def test_counts_tallies_by_class() -> None:
    from setforge.reconcile.hunks import extract_hunks

    hunks = extract_hunks(_BASE, _LIVE)
    tally = counts(hunks)
    assert tally[HunkClass.PENDING] == 2
    assert tally[HunkClass.SHARED] == 0
    assert tally[HunkClass.LOCAL] == 0
