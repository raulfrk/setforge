"""Regression tests for audit finding ``transitions_patch_outcomes``.

Two confirmed defects in :mod:`setforge.transitions`:

1. :func:`compute_patch` produced a malformed ``changes.patch`` for files
   without a trailing newline, because :func:`difflib.unified_diff` never
   emits GNU patch's ``\\ No newline at end of file`` marker. GNU ``patch``
   rejects such a diff (exit 2), so the transition could NEVER be reverted.

2. :func:`load_reconcile_outcomes` read ``reconcile_outcomes.json`` with an
   unguarded ``json.loads``, so a truncated / hand-corrupted file raised a
   bare :class:`json.JSONDecodeError` (not a :class:`SetforgeError`),
   escaping the top-level CLI handler as an opaque traceback.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from setforge.errors import InvalidTransitionRecord
from setforge.transitions import (
    TransitionDir,
    compute_patch,
    load_reconcile_outcomes,
)

# ---------------------------------------------------------------------------
# Finding 1 — no-trailing-newline files must produce a revertible patch
# ---------------------------------------------------------------------------


def _apply_reverse(
    patch_text: str, target: Path, *, dry_run: bool
) -> subprocess.CompletedProcess[str]:
    """Run ``patch -p0 -R`` (optionally ``--dry-run``) on ``patch_text``.

    Mirrors :func:`apply_patch_reverse`'s invocation (``-p0`` + ``-d
    <root>``) so the root-relative paths emitted by :func:`compute_patch`
    resolve. ``target`` is the *root* the diff paths are relative to.
    """
    patch_bin = shutil.which("patch")
    assert patch_bin is not None, "GNU patch not on PATH"
    args = [patch_bin, "-p0", "-R", "-d", str(target), "--reject-file=-"]
    if dry_run:
        args.append("--dry-run")
    return subprocess.run(
        args,
        input=patch_text,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_compute_patch_no_trailing_newline_round_trips(tmp_path: Path) -> None:
    """A file modified from content WITHOUT a trailing newline to content
    WITH one yields a patch that ``patch -R`` accepts and that restores the
    pre-bytes exactly.

    Pre-fix the diff ended with ``-beta+GAMMA`` on one physical line and
    GNU patch rejected it ('malformed patch', exit 2).
    """
    rel = "etc/example.conf"
    pre = "alpha\nbeta"  # no trailing newline
    post = "alpha\nGAMMA\n"  # trailing newline

    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(post, encoding="utf-8")

    patch_text = compute_patch(
        {Path("/" + rel): pre},
        {Path("/" + rel): post},
    )

    # The marker must be present so the diff is well-formed GNU patch.
    assert "\\ No newline at end of file" in patch_text

    dry = _apply_reverse(patch_text, tmp_path, dry_run=True)
    assert dry.returncode == 0, (
        f"dry-run -R failed (exit {dry.returncode}):\n"
        f"{dry.stderr or dry.stdout}\n--- patch ---\n{patch_text}"
    )

    real = _apply_reverse(patch_text, tmp_path, dry_run=False)
    assert real.returncode == 0, (
        f"-R apply failed (exit {real.returncode}):\n{real.stderr or real.stdout}"
    )
    # Byte-exact restoration of the pre-state (still no trailing newline).
    assert target.read_text(encoding="utf-8") == pre


def test_compute_patch_marker_attaches_to_correct_side(tmp_path: Path) -> None:
    """When only the AFTER side lacks a trailing newline, the round-trip
    still restores a pre-state that *had* one — the marker must attach to
    the ``+`` line, not the ``-`` line."""
    rel = "etc/other.conf"
    pre = "one\ntwo\n"  # trailing newline
    post = "one\nXXX"  # no trailing newline

    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(post, encoding="utf-8")

    patch_text = compute_patch(
        {Path("/" + rel): pre},
        {Path("/" + rel): post},
    )
    assert "\\ No newline at end of file" in patch_text

    real = _apply_reverse(patch_text, tmp_path, dry_run=False)
    assert real.returncode == 0, (
        f"-R apply failed (exit {real.returncode}):\n{real.stderr or real.stdout}"
    )
    assert target.read_text(encoding="utf-8") == pre


def test_compute_patch_with_trailing_newline_emits_no_marker(
    tmp_path: Path,
) -> None:
    """Guard against over-eager annotation: when both sides end in a
    newline, no marker is emitted and the diff still round-trips."""
    rel = "etc/clean.conf"
    pre = "a\nb\n"
    post = "a\nB\n"

    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(post, encoding="utf-8")

    patch_text = compute_patch(
        {Path("/" + rel): pre},
        {Path("/" + rel): post},
    )
    assert "\\ No newline at end of file" not in patch_text

    real = _apply_reverse(patch_text, tmp_path, dry_run=False)
    assert real.returncode == 0
    assert target.read_text(encoding="utf-8") == pre


# ---------------------------------------------------------------------------
# Finding 2 + 3 — load_reconcile_outcomes failure branches
# ---------------------------------------------------------------------------


def test_load_reconcile_outcomes_corrupt_json_raises(tmp_path: Path) -> None:
    """A truncated / hand-corrupted ``reconcile_outcomes.json`` raises
    :class:`InvalidTransitionRecord` (a :class:`SetforgeError` subclass),
    NOT a bare :class:`json.JSONDecodeError` that would escape the CLI
    handler as a traceback."""
    (tmp_path / "reconcile_outcomes.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidTransitionRecord):
        load_reconcile_outcomes(TransitionDir(tmp_path))


def test_load_reconcile_outcomes_corrupt_json_is_not_jsondecodeerror(
    tmp_path: Path,
) -> None:
    """Pin the exception type precisely: the raw decode error must be
    wrapped, not leaked."""
    (tmp_path / "reconcile_outcomes.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(InvalidTransitionRecord):
        load_reconcile_outcomes(TransitionDir(tmp_path))
    # And it must NOT surface as the bare decoder error.
    try:
        load_reconcile_outcomes(TransitionDir(tmp_path))
    except json.JSONDecodeError:  # pragma: no cover - regression guard
        pytest.fail("bare json.JSONDecodeError escaped load_reconcile_outcomes")
    except InvalidTransitionRecord:
        pass


def test_load_reconcile_outcomes_non_dict_raises(tmp_path: Path) -> None:
    """A shape-valid-JSON-but-wrong-type top level (a list, not a dict)
    raises :class:`InvalidTransitionRecord` with the documented message."""
    (tmp_path / "reconcile_outcomes.json").write_text("[]", encoding="utf-8")
    with pytest.raises(InvalidTransitionRecord, match="top-level must be a dict"):
        load_reconcile_outcomes(TransitionDir(tmp_path))
