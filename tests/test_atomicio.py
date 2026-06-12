"""Tests for the shared atomic-write primitive."""

import ast
import stat
from pathlib import Path

import pytest

from setforge import atomicio


def test_atomic_write_bytes_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "file.bin"
    payload = b"\x00\x01binary\xffbytes\n"
    atomicio.atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload


def test_atomic_write_text_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    text = "héllo\nwörld\r\n"
    atomicio.atomic_write_text(target, text)
    # Read as bytes so universal-newline translation does not mask the
    # exact-bytes guarantee.
    assert target.read_bytes() == text.encode("utf-8")


def test_atomic_write_bytes_no_fsync_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    atomicio.atomic_write_bytes(target, b"data", fsync=False)
    assert target.read_bytes() == b"data"


def test_atomic_write_tempfile_in_target_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "nested" / "file.bin"
    real_mkstemp = atomicio.tempfile.mkstemp
    seen_dirs: list[str] = []

    def spy_mkstemp(*, dir: str, prefix: str, suffix: str) -> tuple[int, str]:
        seen_dirs.append(dir)
        return real_mkstemp(dir=dir, prefix=prefix, suffix=suffix)

    monkeypatch.setattr(atomicio.tempfile, "mkstemp", spy_mkstemp)
    atomicio.atomic_write_bytes(target, b"x")
    assert seen_dirs == [str(target.parent)]


def test_atomic_write_cleans_temp_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.bin"

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("replace blew up")

    # Fail after the temp file is fully written but before the rename
    # lands, exercising the except-cleanup path.
    monkeypatch.setattr(atomicio.os, "replace", boom)
    with pytest.raises(RuntimeError, match="replace blew up"):
        atomicio.atomic_write_bytes(target, b"payload")

    assert not target.exists()
    leftovers = list(tmp_path.glob(".*.tmp"))
    assert leftovers == []


# --- mode= -----------------------------------------------------------------


def test_mode_applied_to_destination(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    atomicio.atomic_write_bytes(target, b"x", mode=0o755)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_mode_applied_on_text_variant(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    atomicio.atomic_write_text(target, "x", mode=0o640)
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_mode_none_keeps_mkstemp_default(tmp_path: Path) -> None:
    """``mode=None`` applies no perm bits — the 0600 ``mkstemp`` default
    rides through to the destination."""
    target = tmp_path / "file.bin"
    atomicio.atomic_write_bytes(target, b"x")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_no_path_chmod_in_module_source() -> None:
    """Perm bits are set ONLY via ``os.fchmod`` on the temp fd — a
    path-based ``os.chmod`` would re-open the TOCTOU symlink-swap window."""
    src = Path(atomicio.__file__).read_text(encoding="utf-8")
    assert "os.chmod" not in src


def test_atomic_write_bytes_source_orders_fchmod_before_replace() -> None:
    """``os.fchmod`` appears strictly before ``os.replace`` in the source of
    :func:`atomic_write_bytes` — the AST-level proxy for the runtime
    guarantee that perms land on the temp inode before the swap."""
    tree = ast.parse(Path(atomicio.__file__).read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "atomic_write_bytes"
    )
    # Unparse the body WITHOUT the docstring — the prose may legitimately
    # mention os.replace before os.fchmod; the guard is about code order.
    body = fn.body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    src = "\n".join(ast.unparse(stmt) for stmt in body)
    fchmod_idx = src.find("os.fchmod")
    replace_idx = src.find("os.replace")
    assert 0 <= fchmod_idx < replace_idx, (
        "os.fchmod must come before os.replace in atomic_write_bytes source "
        f"(fchmod_idx={fchmod_idx}, replace_idx={replace_idx})"
    )


# --- backup= ---------------------------------------------------------------


def test_backup_returns_bak_path_with_old_content(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n")

    result = atomicio.atomic_write_text(target, "new\n", backup=True)

    assert result == target.with_name(target.name + ".bak")
    assert result.read_text() == "old\n"
    assert target.read_text() == "new\n"


def test_no_backup_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n")
    assert atomicio.atomic_write_text(target, "new\n") is None


def test_backup_overwrites_existing_bak(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n")
    bak = target.with_name(target.name + ".bak")
    bak.write_text("stale backup\n")

    atomicio.atomic_write_text(target, "new\n", backup=True)

    assert bak.read_text() == "old\n"


def test_backup_does_not_follow_preexisting_bak_symlink(tmp_path: Path) -> None:
    """A pre-existing ``.bak`` symlink must be replaced, not written
    through — ``shutil.copy2`` follows symlinks, so without an unlink the
    backup would clobber the link's target instead of snapshotting dst."""
    target = tmp_path / "file.txt"
    target.write_text("old\n")
    victim = tmp_path / "victim"
    victim.write_text("KEEP\n")
    bak = target.with_name(target.name + ".bak")
    bak.symlink_to(victim)

    result = atomicio.atomic_write_text(target, "new\n", backup=True)

    assert victim.read_text() == "KEEP\n"  # target untouched
    assert not bak.is_symlink()  # link replaced by a regular file
    assert bak.read_text() == "old\n"
    assert result == bak


# --- symlink at dst --------------------------------------------------------


def test_symlink_at_dst_replaced_as_entry(tmp_path: Path) -> None:
    """``os.replace`` swaps the symlink ENTRY at dst — the write must never
    go through the link to its target."""
    victim = tmp_path / "victim"
    victim.write_text("KEEP\n")
    target = tmp_path / "file.txt"
    target.symlink_to(victim)

    atomicio.atomic_write_text(target, "new\n")

    assert victim.read_text() == "KEEP\n"  # old target untouched
    assert not target.is_symlink()  # entry replaced by a regular file
    assert target.read_text() == "new\n"


# --- failure-injection cleanup ----------------------------------------------


def test_cleanup_on_fchmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n")

    def boom(fd: int, mode: int) -> None:
        raise OSError("simulated fchmod failure")

    monkeypatch.setattr(atomicio.os, "fchmod", boom)
    with pytest.raises(OSError, match="simulated fchmod failure"):
        atomicio.atomic_write_text(target, "new\n", mode=0o644, backup=True)

    assert target.read_text() == "old\n"  # dst unchanged
    assert list(tmp_path.glob(".*.tmp")) == []
    # fchmod precedes the backup copy, so no .bak was created either.
    assert not target.with_name(target.name + ".bak").exists()


def test_cleanup_on_backup_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n")

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated copy2 failure")

    monkeypatch.setattr(atomicio.shutil, "copy2", boom)
    with pytest.raises(OSError, match="simulated copy2 failure"):
        atomicio.atomic_write_text(target, "new\n", backup=True)

    assert target.read_text() == "old\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_cleanup_on_replace_failure_with_mode_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n")

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(atomicio.os, "replace", boom)
    with pytest.raises(OSError, match="simulated replace failure"):
        atomicio.atomic_write_text(target, "new\n", mode=0o644, backup=True)

    assert target.read_text() == "old\n"
    assert list(tmp_path.glob(".*.tmp")) == []


# --- fsync_path -------------------------------------------------------------


def test_fsync_path_strict_propagates_open_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        atomicio.fsync_path(tmp_path / "missing", strict=True)


def test_fsync_path_strict_propagates_fsync_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x\n")

    def boom(fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(atomicio.os, "fsync", boom)
    with pytest.raises(OSError, match="simulated fsync failure"):
        atomicio.fsync_path(target, strict=True)


def test_fsync_path_non_strict_suppresses(tmp_path: Path) -> None:
    atomicio.fsync_path(tmp_path / "missing", strict=False)  # no raise


def test_fsync_path_succeeds_on_file_and_dir(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x\n")
    atomicio.fsync_path(target, strict=True)
    atomicio.fsync_path(tmp_path, strict=True)
