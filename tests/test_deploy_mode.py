"""Tests for the ``mode=`` kwarg of :func:`setforge.deploy.copy_atomic`
(setforge-8z91).

The contract:

- When ``mode`` is set, ``os.fchmod(tmp_fd, mode)`` runs BEFORE
  ``os.replace`` so the final perm bits are applied atomically with
  the content swap (closes TOCTOU symlink-swap window, bypasses umask).
- When ``mode`` is None, the temp file inherits the source's mode via
  :func:`stat.S_IMODE` — today's behavior, zero regression.
- Setting a tighter mode (e.g. ``0o600`` over a previously-``0o644``
  live file) is honored on UPDATE, not just CREATE.
- fchmod failure is contractual — propagates rather than being
  silently swallowed (the pre-8z91 ``contextlib.suppress(OSError)``
  wrapper is gone).
- AST guarantee: the source of ``_atomic_write`` has ``os.fchmod``
  appearing strictly before ``os.replace`` (the source-text-ordering
  proxy for the runtime guarantee).
"""

import ast
import os
import stat
from pathlib import Path

import pytest

import setforge.deploy as deploy_mod
from setforge.deploy import copy_atomic


def test_mode_kwarg_applied_to_fresh_dst(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.write_text("data\n")
    src.chmod(0o600)
    dst = tmp_path / "dst"

    copy_atomic(src, dst, mode=0o755)

    assert stat.S_IMODE(dst.stat().st_mode) == 0o755


def test_mode_kwarg_overrides_source_mode(tmp_path: Path) -> None:
    """``mode=`` is the authoritative override — source perms are ignored."""
    src = tmp_path / "src"
    src.write_text("data\n")
    src.chmod(0o600)
    dst = tmp_path / "dst"

    copy_atomic(src, dst, mode=0o644)

    assert stat.S_IMODE(dst.stat().st_mode) == 0o644


def test_mode_kwarg_applied_on_update(tmp_path: Path) -> None:
    """An UPDATE deploy (existing dst) also rewrites the mode bits."""
    src = tmp_path / "src"
    src.write_text("new\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    dst.write_text("old\n")
    dst.chmod(0o600)

    copy_atomic(src, dst, mode=0o755)

    assert stat.S_IMODE(dst.stat().st_mode) == 0o755
    assert dst.read_text() == "new\n"


def test_mode_none_falls_back_to_source_mode(tmp_path: Path) -> None:
    """``mode=None`` is the default; perms mirror the source via S_IMODE."""
    src = tmp_path / "src"
    src.write_text("data\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"

    copy_atomic(src, dst)

    assert stat.S_IMODE(dst.stat().st_mode) == 0o644


def test_mode_none_uses_s_imode_not_raw_st_mode(tmp_path: Path) -> None:
    """The fallback path masks file-type bits via :func:`stat.S_IMODE`.

    Without ``S_IMODE``, comparing ``dst.stat().st_mode == 0o644`` would
    always fail because raw ``st_mode`` carries ``S_IFREG`` high-bits.
    The fallback strips those before fchmod.
    """
    src = tmp_path / "src"
    src.write_text("x\n")
    src.chmod(0o640)
    dst = tmp_path / "dst"

    copy_atomic(src, dst)

    # The deployed dst must have ONLY the perm bits, not the source's
    # full st_mode (which contains S_IFREG = 0o100000).
    assert stat.S_IMODE(dst.stat().st_mode) == 0o640
    # Sanity: source's RAW st_mode is NOT 0o640 (carries S_IFREG).
    assert src.stat().st_mode != 0o640


def test_mode_sticky_bit_preserved(tmp_path: Path) -> None:
    """Sticky bit (``0o1000``) is allowed by the validator and applied by fchmod."""
    src = tmp_path / "src"
    src.write_text("x\n")
    src.chmod(0o600)
    dst = tmp_path / "dst"

    copy_atomic(src, dst, mode=0o1755)

    assert stat.S_IMODE(dst.stat().st_mode) == 0o1755


def test_fchmod_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fchmod OSError is contractual — must propagate, not be swallowed.

    The pre-8z91 ``contextlib.suppress(OSError)`` wrapped the
    ``shutil.copystat`` call; the refactored path drops the wrapper.
    """
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"

    def _boom(_fd: int, _mode: int) -> None:
        raise OSError("simulated fchmod failure")

    monkeypatch.setattr(os, "fchmod", _boom)
    with pytest.raises(OSError, match="simulated fchmod failure"):
        copy_atomic(src, dst, mode=0o755)
    # tmp file cleaned despite failure
    leftover = list(tmp_path.glob(".dst.*.tmp"))
    assert leftover == []


def test_mode_only_drift_applied_when_content_identical(tmp_path: Path) -> None:
    """Identical content + drifted mode → UPDATED with perms fixed, no backup."""
    src = tmp_path / "src"
    src.write_text("same\n")
    src.chmod(0o644)
    dst = tmp_path / "dst"
    dst.write_text("same\n")
    dst.chmod(0o600)

    result = copy_atomic(src, dst, mode=0o644)

    assert result.action is deploy_mod.DeployAction.UPDATED
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644
    assert result.backup_path is None
    assert not Path(str(dst) + ".bak").exists()


def test_identical_content_and_mode_stays_noop(tmp_path: Path) -> None:
    """Identical content AND matching mode → still NOOP."""
    src = tmp_path / "src"
    src.write_text("same\n")
    dst = tmp_path / "dst"
    dst.write_text("same\n")
    dst.chmod(0o644)

    result = copy_atomic(src, dst, mode=0o644)

    assert result.action is deploy_mod.DeployAction.NOOP


def test_atomic_write_source_orders_fchmod_before_replace() -> None:
    """The source of :func:`_atomic_write` has ``os.fchmod`` strictly before
    ``os.replace`` — the AST-level proxy for the runtime guarantee that
    perms are applied to the temp inode before it's swapped into place.

    This guards against an accidental reordering refactor that would
    re-open the TOCTOU symlink-swap window.
    """
    tree = ast.parse(Path(deploy_mod.__file__).read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and "atomic" in n.name
    )
    src = ast.unparse(fn)
    fchmod_idx = src.find("os.fchmod")
    replace_idx = src.find("os.replace")
    assert 0 <= fchmod_idx < replace_idx, (
        "os.fchmod must come before os.replace in _atomic_write source "
        f"(fchmod_idx={fchmod_idx}, replace_idx={replace_idx})"
    )


def test_no_shutil_copystat_in_atomic_write() -> None:
    """``shutil.copystat`` is gone — fchmod is the only perm-set path
    (the old copystat+OSError-suppress block was the 8z91 target).
    """
    src = Path(deploy_mod.__file__).read_text(encoding="utf-8")
    assert "shutil.copystat" not in src
