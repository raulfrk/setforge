"""Reverting with an offset leaves no stray backup files."""

import shutil
from pathlib import Path

import pytest

from setforge.transitions import (
    TransitionDir,
    apply_patch_reverse,
    compute_patch,
    snapshot_paths,
)

pytestmark = pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)


def _record(tmp_path: Path, live: Path, before: bytes, after: bytes) -> TransitionDir:
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(before)
    pre = snapshot_paths([live])
    live.write_bytes(after)
    post = snapshot_paths([live])
    transition = TransitionDir(tmp_path / "transition")
    transition.mkdir()
    (transition / "changes.patch").write_text(compute_patch(pre, post))
    return transition


def test_reverse_does_not_leave_backup_files(tmp_path: Path) -> None:
    live = tmp_path / "live.txt"
    before = "".join(f"{n}\n" for n in range(1, 21)).encode()
    after = before.replace(b"15\n", b"fifteen\n")
    transition = _record(tmp_path, live, before, after)
    live.write_bytes(b"extra a\nextra b\n" + after)

    apply_patch_reverse(transition)

    assert live.read_bytes() == b"extra a\nextra b\n" + before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live.txt", "transition"]
