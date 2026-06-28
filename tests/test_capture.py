"""Tests for capture (live → tracked)."""

from pathlib import Path

import pytest

from setforge.capture import (
    CaptureAction,
    capture_profile,
    capture_tracked_file,
)
from setforge.config import Config, Profile, TrackedFile


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_capture_plain_copy(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(dst, "live content\n")
    result = capture_tracked_file(src, dst)
    assert result.action is CaptureAction.UPDATED
    assert src.read_text() == "live content\n"


def test_capture_noop_when_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(src, "same\n")
    _write(dst, "same\n")
    result = capture_tracked_file(src, dst)
    assert result.action is CaptureAction.NOOP


def test_capture_skips_missing_dst(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "missing"
    result = capture_tracked_file(src, dst)
    assert result.action is CaptureAction.SKIPPED
    assert not src.exists()


def test_capture_profile_iterates_tracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src1 = repo / "tracked" / "x"
    src2 = repo / "tracked" / "y"
    dst1 = tmp_path / "live" / "x"
    dst2 = tmp_path / "live" / "y"
    _write(dst1, "x-live\n")
    _write(dst2, "y-live\n")

    config = Config(
        tracked_files={
            "x": TrackedFile(src=Path("x"), dst=str(dst1)),
            "y": TrackedFile(src=Path("y"), dst=str(dst2)),
        },
        profiles={"p": Profile(tracked_files=["x", "y"])},
    )
    # Fresh capture: tracked doesn't exist yet; the walker yields no
    # items, so setforge_yaml_path is required by signature only —
    # not actually read. Pass a placeholder path that doesn't need to
    # exist for this no-drift case.
    results = capture_profile(
        config,
        "p",
        repo,
        setforge_yaml_path=tmp_path / "setforge.yaml",
    )
    assert {r.name for r in results} == {"x", "y"}
    assert all(r.action is CaptureAction.UPDATED for r in results)
    assert src1.read_text() == "x-live\n"
    assert src2.read_text() == "y-live\n"


# --------------------------------------------------------------------------- #
# A5 staged plain-file capture (only SHARED hunks promote into tracked/)
# --------------------------------------------------------------------------- #

_A5_BASE = b"## Tool prefs\nUse rg not grep.\n\n## Host paths\nworkdir: /home/generic\n"
_A5_LIVE = (
    b"## Tool prefs\nUse rg not grep.\n\n"
    b"## Shell\nPrefer zsh.\n\n"
    b"## Host paths\nworkdir: /home/raul\n"
)


def _stage_index(profile: str, fid, base: bytes, live: bytes, classes: dict) -> None:
    """Seed the reconcile base + a staged index for a plain file (simulate a
    prior install + `setforge stage`)."""
    from setforge import locking
    from setforge.reconcile import hunks as H
    from setforge.reconcile import store
    from setforge.reconcile.types import HunkClass

    staged = [
        H.Hunk(
            cls=classes.get(h.label, HunkClass.PENDING),
            label=h.label,
            live_hash=h.live_hash,
            anchor=h.anchor,
            base_span=h.base_span,
            live_span=h.live_span,
        )
        for h in H.extract_hunks(base, live)
    ]
    with locking.profile_lock(profile):
        store.record(profile, fid, base=base, local=live, hunks=H.serialize(staged))


def _a5_config(dst: Path) -> Config:
    return Config(
        tracked_files={"CLAUDE.md": TrackedFile(src=Path("CLAUDE.md"), dst=str(dst))},
        profiles={"p": Profile(tracked_files=["CLAUDE.md"])},
    )


def test_staged_capture_promotes_only_shared_and_keeps_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile import store
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, _A5_BASE.decode())  # tracked currently == upstream base
    _write(dst, _A5_LIVE.decode())  # live carries the host edits

    fid = file_id("CLAUDE.md")
    _stage_index(
        "p",
        fid,
        _A5_BASE,
        _A5_LIVE,
        {"## Shell": HunkClass.SHARED, "## Host paths": HunkClass.LOCAL},
    )

    capture_profile(
        _a5_config(dst), "p", repo, setforge_yaml_path=tmp_path / "setforge.yaml"
    )

    out = src.read_bytes()
    assert b"## Shell" in out  # SHARED promoted
    assert b"Prefer zsh." in out
    assert b"workdir: /home/generic" in out  # LOCAL kept base
    assert b"workdir: /home/raul" not in out  # LOCAL not leaked to tracked
    assert store.read_base("p", fid) == _A5_BASE  # base UNCHANGED on sync
    assert store.reconstruct("p", fid) == _A5_LIVE  # local holds full live (INV-2)
    store.verify("p")


def test_staged_capture_demote_uncaptures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile.types import HunkClass, file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    # tracked already carries the shared Shell block (from a prior sync).
    _write(
        src,
        _A5_LIVE.replace(b"workdir: /home/raul", b"workdir: /home/generic").decode(),
    )
    _write(dst, _A5_LIVE.decode())

    fid = file_id("CLAUDE.md")
    # the host re-stages Shell SHARED -> LOCAL (demote); base still lacks Shell.
    _stage_index("p", fid, _A5_BASE, _A5_LIVE, {"## Shell": HunkClass.LOCAL})

    capture_profile(
        _a5_config(dst), "p", repo, setforge_yaml_path=tmp_path / "setforge.yaml"
    )

    out = src.read_bytes()
    assert b"## Shell" not in out  # demote removed the shared bytes from tracked/
    assert out == _A5_BASE  # tracked back to pure upstream base


def test_staged_capture_pending_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    from setforge.reconcile.types import file_id

    repo = tmp_path / "repo"
    src = repo / "tracked" / "CLAUDE.md"
    dst = tmp_path / "live" / "CLAUDE.md"
    _write(src, _A5_BASE.decode())
    _write(dst, _A5_LIVE.decode())

    fid = file_id("CLAUDE.md")
    _stage_index("p", fid, _A5_BASE, _A5_LIVE, {})  # nothing staged → all PENDING

    results = capture_profile(
        _a5_config(dst), "p", repo, setforge_yaml_path=tmp_path / "setforge.yaml"
    )

    assert src.read_bytes() == _A5_BASE  # PENDING kept local — tracked stays base
    (result,) = [r for r in results if r.name == "CLAUDE.md"]
    assert any("stage" in w for w in result.warnings)  # the "run setforge stage" hint
