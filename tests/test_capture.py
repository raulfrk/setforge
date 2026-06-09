"""Tests for capture (live → tracked)."""

from pathlib import Path

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
