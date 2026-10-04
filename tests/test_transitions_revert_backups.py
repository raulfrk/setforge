"""Reverting with an offset leaves no stray backup files."""

import shutil
from pathlib import Path

import pytest

from setforge.transitions import (
    apply_patch_reverse,
)
from tests.shared_helpers import record_transition

pytestmark = pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)


def test_reverse_does_not_leave_backup_files(tmp_path: Path) -> None:
    live = tmp_path / "live.txt"
    before = "".join(f"{n}\n" for n in range(1, 21)).encode()
    after = before.replace(b"15\n", b"fifteen\n")
    transition = record_transition(tmp_path, live, before, after)
    live.write_bytes(b"extra a\nextra b\n" + after)

    apply_patch_reverse(transition)

    assert live.read_bytes() == b"extra a\nextra b\n" + before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live.txt", "transition"]
