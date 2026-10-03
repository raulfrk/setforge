"""Revert and redo restore recorded files byte-exactly."""

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

_CASES = {
    "crlf": (b"a\r\nb\r\n", b"a\r\nb\r\nc\r\n"),
    "lone-cr": (b"a\rb\r", b"a\rB\r"),
    "mixed": (b"a\r\nb\nc\r", b"a\r\nb\nc\r\nd"),
    "form-feed": (b"one\nsec\x0cond\nthree\n", b"one\nsec\x0cond\nthree\nfour\n"),
    "separators": (
        "x\u2028y\x85z\x1cw\nq\n".encode(),
        "x\u2028y\x85z\x1cw\nq\nr\n".encode(),
    ),
    "nul": (b"a\x00b\nc\n", b"a\x00b\nc\nd\n"),
    "no-final-newline": (b"a\nb", b"a\nb\nc"),
}


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


@pytest.mark.parametrize("name", sorted(_CASES))
def test_reverse_and_redo_restore_exact_bytes(tmp_path: Path, name: str) -> None:
    before, after = _CASES[name]
    live = tmp_path / "live.txt"
    transition = _record(tmp_path, live, before, after)

    apply_patch_reverse(transition)
    assert live.read_bytes() == before

    redo = TransitionDir(tmp_path / "redo")
    redo.mkdir()
    (redo / "changes.patch").write_text(
        compute_patch({live: before.decode()}, {live: after.decode()})
    )
    live.write_bytes(after)
    apply_patch_reverse(redo)
    assert live.read_bytes() == before
