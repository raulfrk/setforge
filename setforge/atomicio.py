"""Shared atomic-write primitive: crash-safe file replacement.

Consumers — the per-host stored-base store (:mod:`setforge.base_store`),
the live-side deploy path (:func:`setforge.deploy._atomic_write`), and the
migration YAML writer (:func:`setforge.migrations._yaml_ops.atomic_write_yaml`) —
need the same durability guarantee: a SIGTERM, power loss, or disk-full
mid-write must never leave the destination truncated or half-written.
The recipe is the standard write-temp-then-rename dance:

1. Write the payload to a sibling temp file in the *same directory* as
   the destination (never ``/tmp`` — a cross-device ``os.replace`` would
   raise ``EXDEV`` and break the atomicity guarantee).
2. ``os.fchmod`` the temp fd when the caller passes explicit ``mode``
   bits — on the fd, never the path, so the perms land on the same FS
   object the rename publishes (no TOCTOU symlink-swap window).
3. ``fsync`` the temp file's data to disk (unless the caller opts out),
   which also covers the fchmod above since it lands on the same fd.
4. Optionally rotate a ``.bak`` sibling (copy of the current
   destination) before the rename.
5. ``os.replace`` the temp file onto the destination — atomic on POSIX.
6. Best-effort ``fsync`` the parent directory so the rename itself is
   durable across a crash.

On any failure the temp file is unlinked so no ``.tmp`` debris leaks. A
process killed between steps 1 and 5 cannot clean up, so every temp file is
named by :func:`temp_name`: directory walks skip such names
(:func:`is_temp_name`) and a later write of the same destination under the
mutation gate removes them.
"""

import contextlib
import ctypes
import errno
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import typer

from setforge import locking
from setforge.errors import SetforgeError

RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
RENAME_FLAGS_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOTSUP, errno.ENOSYS})

# What follows ``.<name>`` in a temp name: 16 random hex digits, with ``u``
# before them when made outside the mutation gate. Kept short because the
# whole name must fit the filesystem's limit beside the destination.
_GATED_TEMP = r"\.setforge-[0-9a-f]{16}\.tmp"
TEMP_SUFFIX = r"\.setforge-u?[0-9a-f]{16}\.tmp"
_TEMP_NAME_RE = re.compile(r"\..+" + TEMP_SUFFIX, re.DOTALL)
_GATED_TEMP_NAME_RE = re.compile(r"\..+" + _GATED_TEMP, re.DOTALL)


def temp_name(name: str) -> str:
    """Return a fresh temporary sibling name for the entry ``name``.

    A name made while the mutation gate is held is ``.<name>.setforge-<hex>.tmp``;
    one made outside it carries ``u`` before the hex. The gate is
    exclusive and dies with its process, so a gate holder that meets the first
    form knows its writer is gone; the second form may belong to a live writer
    and is never removed.
    """
    marker = "setforge-" if locking.mutation_gate_held() else "setforge-u"
    return f".{name}.{marker}{secrets.token_hex(8)}.tmp"


def is_temp_name(name: str) -> bool:
    """Whether ``name`` has either shape :func:`temp_name` produces."""
    return _TEMP_NAME_RE.fullmatch(name) is not None


def is_gated_temp_name(name: str) -> bool:
    """Whether ``name`` is a temp name made while the mutation gate was held."""
    return _GATED_TEMP_NAME_RE.fullmatch(name) is not None


def report_removed_temp(path: Path) -> None:
    """Tell the user on stderr that the leftover temp file ``path`` was removed."""
    typer.secho(
        "warning: removed a temporary file left by an interrupted "
        f"setforge run: {path}",
        err=True,
        fg=typer.colors.YELLOW,
    )


def _sweep_stale_temp_files(path: Path) -> None:
    """Remove temp files a killed gate holder left for the destination ``path``.

    Only under the mutation gate, only regular files in ``path``'s own
    directory, and only the gated names of ``path`` and its ``.bak``. Each
    removal is reported on stderr; a failure is skipped, never raised.
    """
    _sweep_stale(path, r"(?:\.bak)?", stat.S_ISREG)


def sweep_stale_temp_links(path: Path) -> None:
    """Remove staging symlinks a killed gate holder left for the link ``path``.

    Only under the mutation gate, only symlinks in ``path``'s own directory,
    and only the gated names of ``path``; a link is unlinked, never followed,
    so its target is untouched. Reported and tolerated as for a temp file.
    """
    _sweep_stale(path, "", stat.S_ISLNK)


def _sweep_stale(path: Path, infix: str, is_kind: Callable[[int], bool]) -> None:
    if not locking.mutation_gate_held():
        return
    stale = re.compile(re.escape(f".{path.name}") + infix + _GATED_TEMP)
    try:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        for name in sorted(os.listdir(parent_fd)):  # noqa: PTH208 - anchored dirfd
            if stale.fullmatch(name) is None:
                continue
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not is_kind(info.st_mode):
                    continue
                os.unlink(name, dir_fd=parent_fd)
            except OSError:
                continue
            report_removed_temp(path.parent / name)
    except OSError:
        return
    finally:
        os.close(parent_fd)


def _create_temp(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    fsync: bool = True,
    mode: int | None = None,
    backup: bool = False,
) -> Path | None:
    """Atomically write ``data`` to ``path`` via tempfile + ``os.replace``.

    The temp file is created in ``path.parent`` so the rename is a
    same-filesystem operation (atomic on POSIX). When ``fsync`` is true
    the temp file's data is flushed to disk before the rename, and the
    parent directory is fsynced afterward on a best-effort basis so the
    rename survives a crash. On any exception the temp file is removed.

    ``mode`` (keyword-only): explicit permission bits applied to the
    temp fd via ``os.fchmod`` BEFORE the data ``fsync`` (so the mode
    change is covered by the same fsync) and BEFORE the rename.
    ``None`` applies
    nothing — the destination keeps the 0600 temp-file default on a
    fresh write. There is deliberately no shared fallback: the legacy
    writers disagree (deploy copies the SOURCE mode, the migration YAML
    writer preserves the DESTINATION mode), so each call site computes
    its own. An ``os.fchmod`` failure propagates by contract.

    ``backup`` (keyword-only): when true, snapshot the CURRENT
    destination to a sibling ``<name>.bak`` before the rename. The copy is
    staged in a temporary sibling before atomically replacing ``.bak``;
    a failed copy preserves the previous backup, and an existing backup
    symlink is replaced without writing through it. The destination stays
    in place until ``os.replace`` swaps the new content in, so there is
    no window where it is absent. Callers must pass ``backup=True`` only
    when the destination exists.

    Returns the ``.bak`` path when a backup was written, else ``None``.

    A symlink at ``path`` is replaced as a directory ENTRY by
    ``os.replace`` — the write never goes through the link to its
    target (the ``backup`` copy, by contrast, follows it: today's
    deploy contract).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_temp_files(path)
    tmp_path = path.with_name(temp_name(path.name))
    fd = _create_temp(tmp_path)
    backup_path: Path | None = None
    backup_temp: Path | None = None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            if mode is not None:
                os.fchmod(fh.fileno(), mode)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        if backup:
            backup_path = path.with_name(path.name + ".bak")
            backup_temp = path.with_name(temp_name(backup_path.name))
            with os.fdopen(_create_temp(backup_temp), "wb") as backup_file:
                shutil.copy2(path, backup_temp)
                if fsync:
                    os.fsync(backup_file.fileno())
            backup_temp.replace(backup_path)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        if backup_temp is not None:
            backup_temp.unlink(missing_ok=True)
        raise
    if fsync:
        fsync_dir(path.parent)
    return backup_path


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
    mode: int | None = None,
    backup: bool = False,
) -> Path | None:
    """Encode ``text`` and atomically write it to ``path``.

    Thin wrapper over :func:`atomic_write_bytes`: encodes with
    ``encoding`` (default UTF-8) and delegates the durable-replace dance,
    including the ``mode`` / ``backup`` handling and the ``.bak``-path
    return value.
    """
    return atomic_write_bytes(
        path, text.encode(encoding), fsync=fsync, mode=mode, backup=backup
    )


def open_dir_at(
    dir_fd: int,
    parts: Iterable[str],
    *,
    create_mode: int | None = None,
    follow: int = 0,
) -> int:
    """Open the directory ``parts`` below ``dir_fd``, one component at a time.

    Every component is opened without following a symlink, except the first
    ``follow`` ones. With ``create_mode`` a missing component is created
    exclusively and its parent flushed. Returns a new descriptor, a duplicate
    of ``dir_fd`` when ``parts`` is empty; failures raise ``OSError``.
    """
    current = os.dup(dir_fd)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_DIRECTORY
            if index >= follow:
                flags |= os.O_NOFOLLOW
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if create_mode is None:
                    raise
                os.mkdir(part, create_mode, dir_fd=current)
                os.fsync(current)
                child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def names_directory(
    path: str | Path,
    descriptor: int,
    *,
    dir_fd: int | None = None,
    follow_symlinks: bool = False,
) -> bool:
    """Return whether ``path`` still names the directory held as ``descriptor``.

    Device and inode must match; a missing entry or a non-directory does not.
    """
    try:
        live = os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
    except OSError:
        return False
    return stat.S_ISDIR(live.st_mode) and os.path.samestat(live, os.fstat(descriptor))


@contextlib.contextmanager
def staged_file_at(
    parent_fd: int,
    temporary: str,
    data: bytes,
    mode: int,
    *,
    mtime_ns: int | None = None,
) -> Iterator[None]:
    """Hold ``data`` at a new ``temporary`` name while the caller publishes it.

    The name is created exclusively and without following a link, so a
    pre-existing entry is refused and left alone. Mode and ``mtime_ns`` are set
    on the descriptor and flushed with the data before the caller runs; the
    directory is flushed after it. The staged name is removed on exit unless
    publication moved it.
    """
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
        dir_fd=parent_fd,
    )
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written == 0:  # pragma: no cover - kernel contract
                    raise OSError("short write while publishing file")
                view = view[written:]
            os.fchmod(descriptor, mode)
            if mtime_ns is not None:
                os.utime(descriptor, ns=(mtime_ns, mtime_ns))
            os.fsync(descriptor)
        finally:
            # NFS keeps an unlinked name that is still open as a `.nfs*`
            # sibling, which a later directory scan would see; close before
            # publishing.
            os.close(descriptor)
        yield
        os.fsync(parent_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)


def atomic_write_bytes_at(
    parent_fd: int, name: str, data: bytes, *, mode: int = 0o600
) -> None:
    """Atomically replace a regular leaf relative to a held directory fd."""
    temporary = temp_name(name)
    with staged_file_at(parent_fd, temporary, data, mode):
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def renameat2(
    source_fd: int, source: str, destination_fd: int, destination: str, flags: int
) -> None:
    """Rename between held directories with ``RENAME_*`` ``flags``."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise SetforgeError(
            "filesystem publication requires renameat2 support on this platform"
        )
    result = function(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def rename_noreplace_at(parent_fd: int, source: str, destination: str) -> None:
    """Move ``source`` onto an absent ``destination`` inside one held directory.

    An existing destination raises ``FileExistsError`` and is left alone.
    """
    try:
        renameat2(parent_fd, source, parent_fd, destination, RENAME_NOREPLACE)
    except OSError as exc:
        if exc.errno not in RENAME_FLAGS_UNSUPPORTED:
            raise
        # NFS rejects every rename flag.
        rename_onto_claim_at(parent_fd, source, destination)


def rename_onto_claim_at(parent_fd: int, source: str, destination: str) -> None:
    """Exclusively claim an absent destination, then rename over the claim.

    The source moves atomically, an existing destination is refused, and the
    only entry a plain rename can replace is the empty claim made here.
    """
    observed = os.stat(source, dir_fd=parent_fd, follow_symlinks=False)
    directory = stat.S_ISDIR(observed.st_mode)
    if directory:
        os.mkdir(destination, 0o700, dir_fd=parent_fd)
    else:
        os.close(
            os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        )
    try:
        os.rename(source, destination, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException as exc:
        with contextlib.suppress(OSError):
            if directory:
                os.rmdir(destination, dir_fd=parent_fd)
            else:
                os.unlink(destination, dir_fd=parent_fd)
        if isinstance(exc, OSError) and exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            raise FileExistsError(
                errno.EEXIST, os.strerror(errno.EEXIST), destination
            ) from exc
        raise


def fsync_path(path: Path, *, strict: bool) -> None:
    """fsync ``path`` (file or directory) via an ``O_RDONLY`` fd.

    ``strict`` is keyword-only with NO default because the two error
    contracts are opposites and must not be flattened: ``strict=True``
    propagates any ``OSError`` (open or fsync) — used where durability
    is contractual, e.g. snapshot commit markers; ``strict=False``
    swallows it — used where the fsync is a best-effort nicety.
    """
    ctx: contextlib.AbstractContextManager[object] = (
        contextlib.nullcontext() if strict else contextlib.suppress(OSError)
    )
    with ctx:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def fsync_dir(directory: Path) -> None:
    """Best-effort fsync of ``directory`` so a rename is durable.

    Delegates to :func:`fsync_path` with ``strict=False``: any
    ``OSError`` (e.g. a filesystem that rejects directory fsync with
    ``EINVAL``) is swallowed — directory fsync is a durability nicety,
    never a hard requirement; the rename is atomic regardless. Shared by
    every temp-write + atomic-rename site so the dir-fsync recipe is not
    re-implemented per caller.
    """
    fsync_path(directory, strict=False)
