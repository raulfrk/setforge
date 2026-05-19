"""Tests for the ``mode_drift`` channel on :class:`setforge.compare.FileCompare`
(setforge-8z91).

The contract:

- When ``tracked_file.mode`` is ``None``, ``mode_drift`` is always
  False — the drift axis is opt-in per tracked_file.
- When ``tracked_file.mode`` is set and the live dst's permission bits
  (via :func:`stat.S_IMODE`) match, ``mode_drift`` is False AND
  ``status`` is :attr:`CompareStatus.UNCHANGED`.
- When ``tracked_file.mode`` is set and the live dst's perms drift,
  ``mode_drift`` is True AND ``status`` is :attr:`CompareStatus.DRIFTED`.
- ``mode_drift`` contributes to the "unexpected drift" axis that
  ``compare --check`` exits non-zero on (it's a contract violation,
  not user-opt-in drift like preserve_user_keys).
"""

from pathlib import Path

from setforge.compare import (
    CompareStatus,
    _compare_one,
)
from setforge.config import TrackedFile


def _make(src: Path, dst: Path, *, mode: int | None) -> TrackedFile:
    return TrackedFile.model_validate({"src": str(src), "dst": str(dst), "mode": mode})


def test_mode_drift_false_when_mode_unset(tmp_path: Path) -> None:
    """``mode: None`` -> drift axis disabled, always False."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o600)
    tf = _make(src, dst, mode=None)

    entry, _ = _compare_one("foo", src, dst, tf)

    assert entry.mode_drift is False
    assert entry.status is CompareStatus.UNCHANGED


def test_mode_drift_false_when_live_matches_declared(tmp_path: Path) -> None:
    """Declared mode equals live mode -> drift False, UNCHANGED."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o755)
    tf = _make(src, dst, mode=0o755)

    entry, unexpected = _compare_one("foo", src, dst, tf)

    assert entry.mode_drift is False
    assert entry.status is CompareStatus.UNCHANGED
    assert unexpected is False


def test_mode_drift_true_after_manual_chmod(tmp_path: Path) -> None:
    """Declared ``0o755``; user runs ``chmod 0o644`` on dst -> drift detected."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o644)
    tf = _make(src, dst, mode=0o755)

    entry, unexpected = _compare_one("foo", src, dst, tf)

    assert entry.mode_drift is True
    assert entry.status is CompareStatus.DRIFTED
    # mode_drift flows into the "unexpected" axis -> compare --check exits !=0
    assert unexpected is True


def test_mode_drift_uses_s_imode_not_raw_st_mode(tmp_path: Path) -> None:
    """The compare side uses :func:`stat.S_IMODE` to strip ``S_IFREG``
    high-bits — raw ``st_mode`` would always differ from a 12-bit
    perm-only literal.
    """
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o640)
    tf = _make(src, dst, mode=0o640)

    entry, _ = _compare_one("foo", src, dst, tf)

    # The PROOF this works: status is UNCHANGED. A raw st_mode ==
    # comparison would always be False (st_mode carries S_IFREG = 0o100000).
    assert entry.mode_drift is False
    assert entry.status is CompareStatus.UNCHANGED


def test_content_drift_and_mode_drift_compose(tmp_path: Path) -> None:
    """Independent drift axes — both can flag simultaneously."""
    src = tmp_path / "src"
    src.write_text("source-content\n")
    dst = tmp_path / "dst"
    dst.write_text("live-content\n")
    dst.chmod(0o600)
    tf = _make(src, dst, mode=0o755)

    entry, unexpected = _compare_one("foo", src, dst, tf)

    assert entry.diff != ""  # content drift
    assert entry.mode_drift is True  # mode drift
    assert entry.status is CompareStatus.DRIFTED
    assert unexpected is True
