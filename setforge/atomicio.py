"""Shared atomic-write primitive: crash-safe file replacement.

Consumers — the per-host stored-base store (:mod:`setforge.base_store`),
the tracked-side hash-maintenance writer
(:func:`setforge.section_reconcile._atomic_write_text`), the live-side
deploy path (:func:`setforge.deploy._atomic_write`), and the migration
YAML writer (:func:`setforge.migrations._yaml_ops.atomic_write_yaml`) —
need the same durability guarantee: a SIGTERM, power loss, or disk-full
mid-write must never leave the destination truncated or half-written.
The recipe is the standard write-temp-then-rename dance:

1. Write the payload to a sibling temp file in the *same directory* as
   the destination (never ``/tmp`` — a cross-device ``os.replace`` would
   raise ``EXDEV`` and break the atomicity guarantee).
2. ``fsync`` the temp file's data to disk (unless the caller opts out).
3. ``os.fchmod`` the temp fd when the caller passes explicit ``mode``
   bits — on the fd, never the path, so the perms land on the same FS
   object the rename publishes (no TOCTOU symlink-swap window).
4. Optionally rotate a ``.bak`` sibling (copy of the current
   destination) before the rename.
5. ``os.replace`` the temp file onto the destination — atomic on POSIX.
6. Best-effort ``fsync`` the parent directory so the rename itself is
   durable across a crash.

On any failure the temp file is unlinked so no ``.tmp`` debris leaks.
"""

import contextlib
import os
import shutil
import tempfile
from pathlib import Path


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
    temp fd via ``os.fchmod`` BEFORE the rename. ``None`` applies
    nothing — the destination keeps the 0600 ``mkstemp`` default on a
    fresh write. There is deliberately no shared fallback: the legacy
    writers disagree (deploy copies the SOURCE mode, the migration YAML
    writer preserves the DESTINATION mode), so each call site computes
    its own. An ``os.fchmod`` failure propagates by contract.

    ``backup`` (keyword-only): when true, snapshot the CURRENT
    destination to a sibling ``<name>.bak`` before the rename. A
    pre-existing ``.bak`` is unlinked first — ``shutil.copy2`` follows a
    symlink at its destination, so without the unlink a ``.bak`` symlink
    would be written THROUGH instead of replaced. The destination stays
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
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    backup_path: Path | None = None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
            if mode is not None:
                os.fchmod(fh.fileno(), mode)
        if backup:
            backup_path = path.with_name(path.name + ".bak")
            with contextlib.suppress(FileNotFoundError):
                backup_path.unlink()
            shutil.copy2(path, backup_path)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
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
