"""A missing or empty changes.patch cannot report a successful revert."""

import json
import shutil
from pathlib import Path

import pytest

from setforge.errors import RevertFailed
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


def _write_meta(transition: TransitionDir, paths: list[str]) -> None:
    (transition / "meta.json").write_text(json.dumps({"paths": paths}))


@pytest.mark.parametrize("how", ["missing", "empty"])
def test_missing_or_empty_patch_with_recorded_changes_is_refused(
    tmp_path: Path, how: str
) -> None:
    live = tmp_path / "live.txt"
    transition = _record(tmp_path, live, b"one\n", b"two\n")
    patch = transition / "changes.patch"
    if how == "missing":
        patch.unlink()
    else:
        patch.write_text("")
    _write_meta(transition, [str(live)])

    with pytest.raises(RevertFailed, match="nothing was reverted"):
        apply_patch_reverse(transition)
    assert live.read_bytes() == b"two\n"


def test_missing_patch_without_recorded_changes_is_a_noop(tmp_path: Path) -> None:
    transition = TransitionDir(tmp_path / "transition")
    transition.mkdir()
    _write_meta(transition, [])

    apply_patch_reverse(transition)
