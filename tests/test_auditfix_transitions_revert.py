"""Regression tests for audit task ``transitions_revert``.

Two confirmed defects in :mod:`setforge.transitions`:

1. ``compute_patch`` emitted raw (unquoted) ``--- path``/``+++ path``
   headers via :func:`difflib.unified_diff`. GNU ``patch`` terminates a
   filename at the first whitespace, so any tracked destination whose
   path contains a space produced a ``changes.patch`` that
   :func:`apply_patch_reverse` could never apply ("can't find file to
   patch", exit 1) — the forward install succeeded and recorded a
   transition that could NEVER be reverted. The fix C-quotes whitespace
   headers and pairs them with a ``diff --git`` sentinel so GNU patch
   unquotes them; space-free paths stay byte-identical to the historical
   format.

2. :func:`apply_patch_reverse` ran the ``patch`` binary via
   ``subprocess.run`` but only inspected ``returncode``; a
   :class:`subprocess.TimeoutExpired` (patch hangs) or :class:`OSError`
   (binary removed/non-executable after the ``resolve_binary`` TOCTOU
   window) escaped its documented ``RevertFailed`` contract as a raw
   traceback. The fix wraps both passes and re-raises as
   :class:`RevertFailed`, mirroring ``git_ops._run_git``.
"""

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from setforge.errors import RevertFailed
from setforge.transitions import (
    TransitionDir,
    apply_patch_reverse,
    compute_patch,
)

# ---------------------------------------------------------------------------
# Finding 1 — space-containing dst paths must round-trip through revert
# ---------------------------------------------------------------------------


def test_compute_patch_space_path_emits_quoted_git_style_header(
    tmp_path: Path,
) -> None:
    """A path with a space gets a C-quoted header AND a ``diff --git``
    sentinel (without it GNU patch ignores the quoting)."""
    target = tmp_path / "with space" / "f.md"
    patch = compute_patch({target: "before\n"}, {target: "after\n"})

    rel = str(target).lstrip("/")
    assert f'diff --git "{rel}" "{rel}"\n' in patch
    assert f'--- "{rel}"' in patch
    assert f'+++ "{rel}"' in patch
    # The bare (unquoted) header form must NOT appear — that is the bug.
    assert f"--- {rel}\n" not in patch


def test_compute_patch_space_free_path_unchanged_format(tmp_path: Path) -> None:
    """Space-free paths must keep the historical unquoted, sentinel-free
    header so existing transitions and tooling stay byte-compatible."""
    target = tmp_path / "plain.md"
    patch = compute_patch({target: "before\n"}, {target: "after\n"})

    rel = str(target).lstrip("/")
    assert f"--- {rel}\n" in patch
    assert f"+++ {rel}\n" in patch
    assert "diff --git" not in patch
    assert '"' not in patch


@pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not on PATH")
def test_apply_patch_reverse_round_trips_space_path(tmp_path: Path) -> None:
    """The whole-bug repro: install a space-path edit, then revert it.

    Pre-fix the dry-run gate fails with RevertFailed ("can't find file to
    patch"); post-fix the live file is restored to its before-state.
    """
    target = tmp_path / "with space" / "live.md"
    target.parent.mkdir(parents=True)
    target.write_text("after\n", encoding="utf-8")
    transition = TransitionDir(tmp_path / "transition")
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch({target: "before\n"}, {target: "after\n"}),
        encoding="utf-8",
    )

    apply_patch_reverse(transition)

    assert target.read_text() == "before\n"


@pytest.mark.skipif(shutil.which("patch") is None, reason="GNU patch not on PATH")
def test_apply_patch_reverse_round_trips_space_path_create(tmp_path: Path) -> None:
    """A created (``/dev/null`` -> content) space-path file reverses to
    non-existence."""
    target = tmp_path / "with space" / "created.md"
    target.parent.mkdir(parents=True)
    target.write_text("fresh\n", encoding="utf-8")
    transition = TransitionDir(tmp_path / "transition")
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch({target: None}, {target: "fresh\n"}),
        encoding="utf-8",
    )

    apply_patch_reverse(transition)

    assert not target.exists()


# ---------------------------------------------------------------------------
# Finding 2 — patch subprocess failures must surface as RevertFailed
# ---------------------------------------------------------------------------


def _seed_simple_transition(tmp_path: Path) -> TransitionDir:
    target = tmp_path / "live.txt"
    target.write_text("after\n", encoding="utf-8")
    transition = TransitionDir(tmp_path / "transition")
    transition.mkdir()
    (transition / "changes.patch").write_text(
        compute_patch({target: "before\n"}, {target: "after\n"}),
        encoding="utf-8",
    )
    return transition


def test_apply_patch_reverse_timeout_raises_revertfailed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hanging ``patch`` (TimeoutExpired) must become RevertFailed, not
    a raw traceback escaping the revert command boundary."""
    transition = _seed_simple_transition(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["patch"], 60)

    monkeypatch.setattr(subprocess, "run", _boom)

    with pytest.raises(RevertFailed):
        apply_patch_reverse(transition)


def test_apply_patch_reverse_oserror_raises_revertfailed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-executable / vanished ``patch`` binary (OSError) must become
    RevertFailed, mirroring git_ops._run_git's TOCTOU handling."""
    transition = _seed_simple_transition(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise PermissionError("patch not executable")

    monkeypatch.setattr(subprocess, "run", _boom)

    with pytest.raises(RevertFailed):
        apply_patch_reverse(transition)


def test_apply_patch_reverse_timeout_on_real_apply_raises_revertfailed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout on the SECOND (real-apply) pass — after a clean dry-run —
    must also surface as RevertFailed rather than a traceback."""
    transition = _seed_simple_transition(tmp_path)

    if shutil.which("patch") is None:
        pytest.skip("GNU patch not on PATH")

    real_run = subprocess.run
    calls = {"n": 0}

    def _flaky(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            # Let the dry-run pass through to the real binary.
            return real_run(args, **kwargs)
        raise subprocess.TimeoutExpired(["patch"], 60)

    monkeypatch.setattr(subprocess, "run", _flaky)

    with pytest.raises(RevertFailed):
        apply_patch_reverse(transition)
