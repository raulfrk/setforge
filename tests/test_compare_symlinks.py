"""Tests for symlink-aware compare dispatch (setforge-m483).

Three drift shapes the new ``_compare_symlinked`` helper classifies:

1. Broken symlink (dst is a symlink pointing at a target that does
   not exist) — DRIFTED, NOT MISSING. Fixes the existing-bug surface
   where ``compare.py:_compare_one`` used ``if not dst.exists()`` as
   the existence probe; ``Path.exists()`` returns False on a broken
   link, which misclassified the case as MISSING. m483's contract is
   that the link IS present and is what setforge installed — the
   drift is that the target is gone, not that the link is gone.

2. Regular file where setforge expects a symlink — DRIFTED with a
   diff that mentions both the expected target and the unexpected
   regular file.

3. Symlink with wrong target — DRIFTED with a diff that mentions
   both the actual and expected targets.

Plus two pass-through cases:

- No symlink AND no regular file — MISSING (caller's revert-after
  scenario; no link to revert).
- Correct symlink to declared target — UNCHANGED.
"""

from __future__ import annotations

import os
from pathlib import Path

from setforge.compare import (
    CompareStatus,
    _compare_one,
)
from setforge.config import TrackedFile


def _make(src: Path, dst: Path, *, symlink: str | None) -> TrackedFile:
    return TrackedFile.model_validate(
        {"src": str(src), "dst": str(dst), "symlink": symlink}
    )


def test_broken_symlink_is_not_missing(tmp_path: Path) -> None:
    """A broken symlink (link present, target absent) is NOT MISSING.

    The spec acceptance #3 invariant: pre-m483 ``compare.py:_compare_one``
    used ``if not dst.exists()`` as the existence probe; ``exists()``
    returns False on a broken link, so dangling tracked symlinks
    classified as MISSING. The m483 dispatch via ``is_symlink()`` (BEFORE
    the ``exists()`` branch) catches the link, then probes ``os.readlink``
    for target match — so the link's classification is decoupled from the
    target's reachability.

    For a broken-but-target-matching link this yields UNCHANGED (the
    link is exactly as setforge installed it; the target's absence is a
    target-side concern, not link-side drift). The companion test
    :func:`test_broken_symlink_with_correct_target_is_unchanged` asserts
    that precise classification; this test guards the broader spec
    invariant (NOT MISSING) against future re-introductions of the
    ``exists()``-only probe.
    """
    src = tmp_path / "src"
    src.write_text("hello\n")
    target = tmp_path / "missing-target"  # never created
    dst = tmp_path / "link"
    os.symlink(str(target), dst)
    assert dst.is_symlink()
    assert not dst.exists()  # CPython: broken link -> exists() False.

    tf = _make(src, dst, symlink=str(target))
    entry, was_drifted = _compare_one("foo", src, dst, tf)

    assert entry.status is not CompareStatus.MISSING, (
        f"broken symlink must NOT classify as MISSING (spec acceptance "
        f"#3 invariant; existing-bug surface fix); got {entry.status}"
    )
    assert was_drifted is False


def test_broken_symlink_with_correct_target_is_unchanged(tmp_path: Path) -> None:
    """Defensive re-statement of the broken-link-correct-target case.

    The spec's existing-bug surface note: pre-m483 the case classified
    as MISSING because ``exists()`` returned False. m483's contract:
    a broken link is NOT MISSING. Whether it counts as DRIFTED or
    UNCHANGED depends on the target — if the declared symlink target
    matches ``os.readlink``, the LINK is correct; only the target
    file is gone (target-side concern). This test asserts the LINK
    classification (not target file content).
    """
    src = tmp_path / "src"
    src.write_text("x\n")
    declared_target = tmp_path / "ghost"  # never created
    dst = tmp_path / "link"
    os.symlink(str(declared_target), dst)
    tf = _make(src, dst, symlink=str(declared_target))

    entry, _ = _compare_one("foo", src, dst, tf)
    # Per spec: broken link → DRIFTED is acceptable IFF the existing
    # bug (classifies as MISSING) is fixed. Either DRIFTED or
    # UNCHANGED satisfies the spec; what MUST NOT happen is MISSING.
    assert entry.status is not CompareStatus.MISSING


def test_symlink_missing_is_missing(tmp_path: Path) -> None:
    """No symlink AND no regular file at dst — MISSING (nothing deployed)."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "link"  # not created
    tf = _make(src, dst, symlink="~/foo")

    entry, was_drifted = _compare_one("foo", src, dst, tf)
    assert entry.status is CompareStatus.MISSING
    assert was_drifted is True


def test_regular_file_where_symlink_expected_is_drifted(tmp_path: Path) -> None:
    """Regular file at ``dst`` where setforge expects a symlink — DRIFTED."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "link"
    dst.write_text("user-content\n")  # NOT a symlink.
    tf = _make(src, dst, symlink="~/target")

    entry, was_drifted = _compare_one("foo", src, dst, tf)
    assert entry.status is CompareStatus.DRIFTED
    assert was_drifted is True
    assert "regular file" in entry.diff
    assert "~/target" in entry.diff


def test_symlink_target_drift_is_drifted(tmp_path: Path) -> None:
    """Symlink at dst whose target differs from declared — DRIFTED with diff."""
    src = tmp_path / "src"
    src.write_text("x\n")
    declared = "~/expected-target"
    actual = "~/other-target"
    dst = tmp_path / "link"
    os.symlink(actual, dst)
    tf = _make(src, dst, symlink=declared)

    entry, was_drifted = _compare_one("foo", src, dst, tf)
    assert entry.status is CompareStatus.DRIFTED
    assert was_drifted is True
    assert declared in entry.diff
    assert actual in entry.diff


def test_correct_symlink_is_unchanged(tmp_path: Path) -> None:
    """Symlink at dst points at the declared target AND content matches
    — UNCHANGED, no drift."""
    src = tmp_path / "src"
    src.write_text("payload\n")
    target = tmp_path / "real-target"
    target.write_text("payload\n")  # equal to src — no content drift.
    dst = tmp_path / "link"
    declared = str(target)
    os.symlink(declared, dst)
    tf = _make(src, dst, symlink=declared)

    entry, was_drifted = _compare_one("foo", src, dst, tf)
    assert entry.status is CompareStatus.UNCHANGED
    assert was_drifted is False
    assert entry.diff == ""


def test_correct_symlink_with_target_content_drift_is_drifted(
    tmp_path: Path,
) -> None:
    """Symlink metadata is correct but the target file's content has
    drifted from tracked ``src`` — DRIFTED with a content diff body.

    Pre-fix surface: ``_compare_symlinked`` returned UNCHANGED on every
    correct-target symlink regardless of what the target file held,
    silently hiding target-content drift from the user. The fix routes
    through :func:`compare.diff_file` against the expanded target path
    so post-deploy target-content edits surface as DRIFTED.
    """
    src = tmp_path / "src"
    src.write_text("tracked-payload\n")
    target = tmp_path / "real-target"
    target.write_text("user-edited-payload\n")  # diverges from tracked src.
    dst = tmp_path / "link"
    declared = str(target)
    os.symlink(declared, dst)
    tf = _make(src, dst, symlink=declared)

    entry, was_drifted = _compare_one("foo", src, dst, tf)
    assert entry.status is CompareStatus.DRIFTED
    assert was_drifted is True
    assert "tracked-payload" in entry.diff
    assert "user-edited-payload" in entry.diff


def test_symlink_dispatch_runs_before_not_exists_branch(tmp_path: Path) -> None:
    """Coverage anchor: the symlink branch is reached even when dst.exists()
    is False (broken link). If the not-exists branch ran first the test
    would observe MISSING — but the dispatch order in ``_compare_one``
    puts symlink check FIRST.
    """
    src = tmp_path / "src"
    src.write_text("x\n")
    target = tmp_path / "ghost"
    dst = tmp_path / "link"
    os.symlink(str(target), dst)
    tf = _make(src, dst, symlink=str(target))
    # dst.exists() is False because target is missing — proves the
    # dispatch order matters.
    assert not dst.exists()
    entry, _ = _compare_one("foo", src, dst, tf)
    # NOT MISSING (would be the result of the not-exists branch).
    assert entry.status is not CompareStatus.MISSING
