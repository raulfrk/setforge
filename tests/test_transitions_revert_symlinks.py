"""Revert resolves symlinked directories and destinations before patching."""

import shutil
from pathlib import Path

import pytest

from setforge.transitions import (
    TransitionDir,
    apply_patch_reverse,
    compute_patch,
    summarize_transition,
)
from tests.shared_helpers import record_transition

pytestmark = pytest.mark.skipif(
    shutil.which("patch") is None, reason="GNU patch not on PATH"
)


def test_header_like_body_lines_are_not_headers(tmp_path: Path) -> None:
    live = tmp_path / "live.txt"
    transition = record_transition(
        tmp_path, live, b"-- a\n++ b\nx\n", b"-- a\n++ b\nx\ny\n"
    )

    assert summarize_transition(transition) == {str(live): "modified"}
    apply_patch_reverse(transition)
    assert live.read_bytes() == b"-- a\n++ b\nx\n"


def test_reverse_through_symlinked_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    live = tmp_path / "link" / "sub" / "f.txt"
    transition = record_transition(tmp_path, live, b"one\n", b"one\ntwo\n")

    apply_patch_reverse(transition)

    assert (real / "sub" / "f.txt").read_bytes() == b"one\n"


def test_reverse_creation_through_symlinked_directory(tmp_path: Path) -> None:
    real = tmp_path / "real with space"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    live = tmp_path / "link" / "f.txt"
    live.write_text("fresh\n")
    transition = TransitionDir(tmp_path / "transition")
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch({live: None}, {live: "fresh\n"})
    )

    apply_patch_reverse(transition)

    assert not (real / "f.txt").exists()


def test_reverse_quoted_path_resolving_to_plain_path(tmp_path: Path) -> None:
    real = tmp_path / "plain"
    real.mkdir()
    (tmp_path / "with space").symlink_to(real)
    live = tmp_path / "with space" / "f.txt"
    transition = record_transition(tmp_path, live, b"one\n", b"one\ntwo\n")

    apply_patch_reverse(transition)

    assert (real / "f.txt").read_bytes() == b"one\n"


def test_reverse_when_destination_is_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    live = tmp_path / "live.txt"
    live.symlink_to(target)
    transition = record_transition(tmp_path, live, b"one\n", b"one\ntwo\n")

    apply_patch_reverse(transition)

    assert live.is_symlink()
    assert target.read_bytes() == b"one\n"
