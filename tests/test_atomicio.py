"""Tests for the shared atomic-write primitive."""

import ast
import contextlib
import errno
import os
import stat
from pathlib import Path

import pytest

from setforge import atomicio
from setforge.locking import mutation_locks


def test_atomic_write_bytes_at_stays_bound_after_parent_symlink_swap(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "native"
    parent.mkdir()
    moved = tmp_path / "moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parent.rename(moved)
        parent.symlink_to(outside, target_is_directory=True)

        atomicio.atomic_write_bytes_at(parent_fd, "config.toml", b"safe = true\n")
    finally:
        os.close(parent_fd)

    assert (moved / "config.toml").read_bytes() == b"safe = true\n"
    assert not (outside / "config.toml").exists()


def test_staged_file_at_holds_exact_bytes_mode_and_mtime_until_published(
    tmp_path: Path,
) -> None:
    mtime_ns = 1_700_000_000_123_456_789
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with atomicio.staged_file_at(
            parent_fd, ".staged", b"payload\n", 0o640, mtime_ns=mtime_ns
        ):
            staged = (tmp_path / ".staged").stat()
            assert (tmp_path / ".staged").read_bytes() == b"payload\n"
            os.replace(".staged", "leaf", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    finally:
        os.close(parent_fd)

    published = (tmp_path / "leaf").stat()
    assert stat.S_IMODE(staged.st_mode) == 0o640
    assert (stat.S_IMODE(published.st_mode), published.st_mtime_ns) == (
        0o640,
        mtime_ns,
    )
    assert published.st_ino == staged.st_ino
    assert sorted(path.name for path in tmp_path.iterdir()) == ["leaf"]


def test_staged_file_at_removes_the_staged_name_when_publication_fails(
    tmp_path: Path,
) -> None:
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with (
            pytest.raises(RuntimeError, match="publish failed"),
            atomicio.staged_file_at(parent_fd, ".staged", b"payload\n", 0o600),
        ):
            raise RuntimeError("publish failed")
    finally:
        os.close(parent_fd)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("occupant", ["file", "symlink"])
def test_staged_file_at_refuses_and_keeps_an_existing_staged_name(
    tmp_path: Path, occupant: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside\n")
    staged = root / ".staged"
    if occupant == "file":
        staged.write_bytes(b"theirs\n")
    else:
        staged.symlink_to(outside)
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with (
            pytest.raises(FileExistsError),
            atomicio.staged_file_at(parent_fd, ".staged", b"ours\n", 0o600),
        ):
            pytest.fail("the staged name was not created by this call")
    finally:
        os.close(parent_fd)

    assert outside.read_bytes() == b"outside\n"
    if occupant == "file":
        assert staged.read_bytes() == b"theirs\n"
    else:
        assert staged.is_symlink()


def _open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))  # noqa: PTH208 - descriptor table


def test_open_dir_at_returns_a_new_descriptor_for_the_named_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        nested_fd = atomicio.open_dir_at(root_fd, ("a", "b"))
        same_fd = atomicio.open_dir_at(root_fd, ())
        try:
            assert os.path.samestat(os.fstat(nested_fd), (tmp_path / "a" / "b").stat())
            assert os.path.samestat(os.fstat(same_fd), tmp_path.stat())
            assert len({root_fd, nested_fd, same_fd}) == 3
        finally:
            os.close(nested_fd)
            os.close(same_fd)
    finally:
        os.close(root_fd)


@pytest.mark.parametrize(
    ("link", "parts", "follow"),
    [
        ("a", ("a", "b"), 0),
        ("a/b", ("a", "b"), 0),
        ("a/b", ("a", "b"), 1),
    ],
)
def test_open_dir_at_refuses_a_symlink_component_it_may_not_follow(
    tmp_path: Path, link: str, parts: tuple[str, ...], follow: int
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (outside / "b").mkdir(parents=True)
    root.mkdir()
    if link == "a/b":
        (root / "a").mkdir()
    (root / link).symlink_to(outside, target_is_directory=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    before = _open_descriptors()
    try:
        with pytest.raises(OSError) as raised:  # noqa: PT011 - errno checked below
            atomicio.open_dir_at(root_fd, parts, create_mode=0o700, follow=follow)
        assert _open_descriptors() == before
    finally:
        os.close(root_fd)

    assert raised.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert sorted(path.name for path in outside.iterdir()) == ["b"]


def test_open_dir_at_follows_only_the_leading_components_it_is_told_to(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    (real / "store").mkdir(parents=True)
    (tmp_path / "home").symlink_to(real, target_is_directory=True)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        store_fd = atomicio.open_dir_at(root_fd, ("home", "store"), follow=1)
        try:
            assert os.path.samestat(os.fstat(store_fd), (real / "store").stat())
        finally:
            os.close(store_fd)
    finally:
        os.close(root_fd)


def test_open_dir_at_creates_only_missing_components_and_only_when_asked(
    tmp_path: Path,
) -> None:
    (tmp_path / "kept").mkdir(mode=0o755)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    before = _open_descriptors()
    try:
        with pytest.raises(FileNotFoundError):
            atomicio.open_dir_at(root_fd, ("kept", "new", "leaf"))
        assert _open_descriptors() == before
        assert list((tmp_path / "kept").iterdir()) == []

        leaf_fd = atomicio.open_dir_at(
            root_fd, ("kept", "new", "leaf"), create_mode=0o700
        )
        os.close(leaf_fd)
    finally:
        os.close(root_fd)

    modes = [
        stat.S_IMODE((tmp_path / relative).stat().st_mode)
        for relative in ("kept", "kept/new", "kept/new/leaf")
    ]
    assert modes == [0o755, 0o700, 0o700]


def test_names_directory_tracks_the_held_directory_not_the_name(
    tmp_path: Path,
) -> None:
    held = tmp_path / "held"
    held.mkdir()
    descriptor = os.open(held, os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert atomicio.names_directory(held, descriptor)
        assert atomicio.names_directory("held", descriptor, dir_fd=parent_fd)

        held.rename(tmp_path / "moved")
        assert not atomicio.names_directory(held, descriptor)
        assert atomicio.names_directory("moved", descriptor, dir_fd=parent_fd)

        held.mkdir()
        assert not atomicio.names_directory(held, descriptor)
        assert not atomicio.names_directory("held", descriptor, dir_fd=parent_fd)

        held.rmdir()
        held.symlink_to(tmp_path / "moved", target_is_directory=True)
        assert not atomicio.names_directory(held, descriptor)
        assert atomicio.names_directory(held, descriptor, follow_symlinks=True)

        held.unlink()
        held.write_bytes(b"")
        assert not atomicio.names_directory(held, descriptor, follow_symlinks=True)
    finally:
        os.close(parent_fd)
        os.close(descriptor)


def _reject_rename_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(
        source_fd: int, source: str, destination_fd: int, destination: str, flags: int
    ) -> None:
        del source_fd, source, destination_fd, flags
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), destination)

    monkeypatch.setattr(atomicio, "renameat2", reject)


def _make_entry(path: Path, kind: str, marker: str) -> None:
    if kind == "directory":
        path.mkdir()
        (path / marker).write_bytes(b"")
    else:
        path.write_text(marker, encoding="utf-8")


def _entry(path: Path) -> object:
    if path.is_dir():
        return sorted(child.name for child in path.iterdir())
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("flags", ["supported", "rejected"])
@pytest.mark.parametrize("kind", ["directory", "file"])
def test_rename_noreplace_at_moves_onto_an_absent_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: str, kind: str
) -> None:
    _make_entry(tmp_path / "source", kind, "ours")
    if flags == "rejected":
        _reject_rename_flags(monkeypatch)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        atomicio.rename_noreplace_at(parent_fd, "source", "destination")
    finally:
        os.close(parent_fd)

    assert [path.name for path in tmp_path.iterdir()] == ["destination"]
    assert _entry(tmp_path / "destination") == (
        ["ours"] if kind == "directory" else "ours"
    )


@pytest.mark.parametrize("flags", ["supported", "rejected"])
@pytest.mark.parametrize("kind", ["directory", "file"])
@pytest.mark.parametrize("occupant", ["directory", "file"])
def test_rename_noreplace_at_refuses_and_keeps_an_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flags: str,
    kind: str,
    occupant: str,
) -> None:
    _make_entry(tmp_path / "source", kind, "ours")
    _make_entry(tmp_path / "destination", occupant, "theirs")
    if flags == "rejected":
        _reject_rename_flags(monkeypatch)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            atomicio.rename_noreplace_at(parent_fd, "source", "destination")
    finally:
        os.close(parent_fd)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "destination",
        "source",
    ]
    assert _entry(tmp_path / "source") == (["ours"] if kind == "directory" else "ours")
    assert _entry(tmp_path / "destination") == (
        ["theirs"] if occupant == "directory" else "theirs"
    )


def test_rename_onto_claim_at_reports_a_claim_filled_by_someone_else_as_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "source").mkdir()
    real_rename = os.rename

    def fill_claim_then_rename(source: str, destination: str, **kwargs: int) -> None:
        (tmp_path / destination / "theirs").write_bytes(b"")
        real_rename(source, destination, **kwargs)

    monkeypatch.setattr(os, "rename", fill_claim_then_rename)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError) as raised:
            atomicio.rename_onto_claim_at(parent_fd, "source", "destination")
    finally:
        os.close(parent_fd)

    assert raised.value.errno == errno.EEXIST
    assert (tmp_path / "source").is_dir()
    assert _entry(tmp_path / "destination") == ["theirs"]


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
    real_replace = os.replace
    staged: list[Path] = []

    def spy_replace(src: Path, dst: Path) -> None:
        staged.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr(atomicio.os, "replace", spy_replace)
    atomicio.atomic_write_bytes(target, b"x")
    (temporary,) = staged
    assert temporary.parent == target.parent
    assert atomicio.is_temp_name(temporary.name)


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
    """``os.fchmod`` appears strictly before the atomic swap in the source of
    :func:`atomic_write_bytes` — the AST-level proxy for the runtime
    guarantee that perms land on the temp inode before the swap.

    The swap is spelled ``<tmp>.replace(<dst>)`` (pathlib) rather than
    ``os.replace``; both lower to the same ``os.replace`` syscall, so the
    guard matches ``.replace(`` to stay refactor-tolerant on the call form.
    """
    tree = ast.parse(Path(atomicio.__file__).read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "atomic_write_bytes"
    )
    # Unparse the body WITHOUT the docstring — the prose may legitimately
    # mention the swap before os.fchmod; the guard is about code order.
    body = fn.body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    src = "\n".join(ast.unparse(stmt) for stmt in body)
    fchmod_idx = src.find("os.fchmod")
    replace_idx = src.find(".replace(")
    assert 0 <= fchmod_idx < replace_idx, (
        "os.fchmod must come before the .replace() swap in atomic_write_bytes "
        f"source (fchmod_idx={fchmod_idx}, replace_idx={replace_idx})"
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


@pytest.mark.parametrize("backup_is_symlink", [False, True])
def test_cleanup_on_backup_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_is_symlink: bool
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("old\n")
    backup = target.with_name("file.txt.bak")
    previous = tmp_path / "previous"
    previous.write_text("recovery copy\n")
    if backup_is_symlink:
        backup.symlink_to(previous)
    else:
        backup.write_text("recovery copy\n")

    def boom(src: object, dst: object) -> None:
        assert isinstance(dst, Path)
        dst.write_bytes(b"partial backup")
        raise OSError("simulated copy2 failure")

    monkeypatch.setattr(atomicio.shutil, "copy2", boom)
    with pytest.raises(OSError, match="simulated copy2 failure"):
        atomicio.atomic_write_text(target, "new\n", backup=True)

    assert target.read_text() == "old\n"
    assert backup.read_text() == "recovery copy\n"
    assert backup.is_symlink() is backup_is_symlink
    assert previous.read_text() == "recovery copy\n"
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


@pytest.mark.parametrize(
    ("gate_held", "backup", "longest"),
    [(True, False, 224), (True, True, 220), (False, False, 223), (False, True, 219)],
)
def test_longest_destination_name_an_atomic_write_accepts(
    tmp_path: Path, gate_held: bool, backup: bool, longest: int
) -> None:
    """The temp name must fit the 255-byte name limit beside the destination."""
    gate = mutation_locks() if gate_held else contextlib.nullcontext()
    fits = tmp_path / ("n" * longest)
    too_long = tmp_path / ("m" * (longest + 1))
    if backup:
        fits.write_bytes(b"old\n")
        too_long.write_bytes(b"old\n")

    with gate:
        atomicio.atomic_write_bytes(fits, b"new\n", backup=backup)
        with pytest.raises(OSError, match="File name too long") as raised:
            atomicio.atomic_write_bytes(too_long, b"new\n", backup=backup)

    assert fits.read_bytes() == b"new\n"
    assert raised.value.errno == errno.ENAMETOOLONG
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [fits.name, *([f"{fits.name}.bak", too_long.name] if backup else [])]
    )
