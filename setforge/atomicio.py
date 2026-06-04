"""Shared atomic-write primitive: crash-safe file replacement.

Consumers — the per-host stored-base store (:mod:`setforge.base_store`)
and the tracked-side hash-maintenance writer
(:func:`setforge.section_reconcile._atomic_write_text`) — need the same
durability guarantee: a SIGTERM, power loss, or disk-full mid-write must
never leave the destination truncated or half-written. (The live-side
deploy path, :func:`setforge.deploy._atomic_write`, implements the same
recipe independently — it carries extra ``fchmod`` / ``.bak`` / symlink
handling and is not yet migrated onto this primitive.) The recipe is the
standard write-temp-then-rename dance:

1. Write the payload to a sibling temp file in the *same directory* as
   the destination (never ``/tmp`` — a cross-device ``os.replace`` would
   raise ``EXDEV`` and break the atomicity guarantee).
2. ``fsync`` the temp file's data to disk (unless the caller opts out).
3. ``os.replace`` the temp file onto the destination — atomic on POSIX.
4. Best-effort ``fsync`` the parent directory so the rename itself is
   durable across a crash.

On any failure the temp file is unlinked so no ``.tmp`` debris leaks.
"""

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, fsync: bool = True) -> None:
    """Atomically write ``data`` to ``path`` via tempfile + ``os.replace``.

    The temp file is created in ``path.parent`` so the rename is a
    same-filesystem operation (atomic on POSIX). When ``fsync`` is true
    the temp file's data is flushed to disk before the rename, and the
    parent directory is fsynced afterward on a best-effort basis so the
    rename survives a crash. On any exception the temp file is removed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    if fsync:
        _fsync_dir(path.parent)


def atomic_write_text(
    path: Path, text: str, *, encoding: str = "utf-8", fsync: bool = True
) -> None:
    """Encode ``text`` and atomically write it to ``path``.

    Thin wrapper over :func:`atomic_write_bytes`: encodes with
    ``encoding`` (default UTF-8) and delegates the durable-replace dance.
    """
    atomic_write_bytes(path, text.encode(encoding), fsync=fsync)


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of ``directory`` so a rename is durable.

    Opens the directory ``O_RDONLY``, fsyncs it, and closes it. Any
    ``OSError`` (e.g. a filesystem that rejects directory fsync) is
    swallowed — this is a durability nicety, never a hard requirement.
    """
    with contextlib.suppress(OSError):
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
