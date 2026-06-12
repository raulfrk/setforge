"""Filesystem helpers shared by migrations.

Migrations may rewrite YAML, markdown, or any other tracked-content
file. They MUST use the helpers in this module rather than in-place
``open(..., "w")`` writes so that a crash mid-apply never leaves a
half-written file on disk and so that every touched file has a sibling
``.pre-<to_version>.bak`` snapshot the user can roll back to.

The split from :mod:`setforge.migrations._yaml_ops` is intentional —
YAML round-trip helpers depend on ruamel, while these helpers only
depend on the stdlib and can be used for tracked-content sweeps
(markdown, plain-text sentinels, JSON manifests, etc).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

__all__ = ["atomic_replace", "backup_path", "iter_tracked_text_files"]


# Binary file suffixes excluded from ``iter_tracked_text_files``.
# Migrations that need to touch binary content should reach for the
# specific file path directly — there is no use case yet for sweeping
# binary content under tracked/.
_BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".whl",
        ".egg",
        ".so",
        ".dylib",
        ".dll",
        ".class",
        ".pyc",
        ".pyo",
        ".o",
        ".a",
    }
)


def backup_path(p: Path, to_version: str) -> Path:
    """Return the sibling backup path for ``p`` under target ``to_version``.

    Format: ``<p.name>.pre-<to_version>.bak``. Lives in the same
    directory as ``p`` so the user finds it next to the migrated file
    and rolls back with a single ``mv``.
    """
    return p.with_name(f"{p.name}.pre-{to_version}.bak")


def atomic_replace(src_tmp: Path, dst: Path) -> None:
    """Atomically move ``src_tmp`` onto ``dst`` via ``os.replace``.

    Requires both paths to be on the same filesystem (the standard
    constraint for atomic-rename semantics). Migrations always stage
    the tmp file in ``dst.parent`` to satisfy this.

    Deliberately a bare ``os.replace`` with NO fsync: the caller has
    already staged the tmp file, so there is no write for
    :mod:`setforge.atomicio` to wrap (its writers create their own
    temp), and this helper has never fsynced — adding durability here
    would silently change migration behavior.
    """
    os.replace(src_tmp, dst)


def iter_tracked_text_files(repo_root: Path) -> Iterator[Path]:
    """Yield every text file under ``repo_root`` a migration might edit.

    Excludes the ``.git`` directory and any path whose suffix is in
    :data:`_BINARY_SUFFIXES`. Used by migrations that need to sweep
    tracked content (e.g. renaming a user-section marker namespace
    across every ``tracked/`` markdown file).

    Order: depth-first, deterministic via ``sorted(p.iterdir())`` so
    the diff preview is reproducible.
    """
    yield from _iter_text_files(repo_root)


def _iter_text_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        if entry.name == ".git":
            continue
        if entry.is_dir():
            yield from _iter_text_files(entry)
            continue
        if entry.suffix.lower() in _BINARY_SUFFIXES:
            continue
        if entry.is_file():
            yield entry
