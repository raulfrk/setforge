"""Filesystem helpers shared by migrations.

Migrations may rewrite YAML, markdown, or any other tracked-content
file. They MUST use the helpers in this module rather than in-place
``open(..., "w")`` writes so that a crash mid-apply never leaves a
half-written file on disk and so that every touched file has a sibling
``.pre-<to_version>.bak`` snapshot the user can roll back to.

The split from :mod:`setforge.migrations._yaml_ops` is intentional —
YAML round-trip helpers depend on ruamel, while these helpers only
depend on the stdlib.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["atomic_replace", "backup_path"]


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
    src_tmp.replace(dst)
