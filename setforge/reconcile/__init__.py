"""Reconcile engine: the base/local/index store + the line-level 3-way merge.

Stable public façade — call sites import ``from setforge.reconcile import ...``;
the submodules (:mod:`~setforge.reconcile.store`, ``index_model``, ``merge``,
``merge_model``, ``types``, ``wizard``) are internal and may be refactored
without touching consumers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from setforge.reconcile.host_local_record import (
    record_local_reloc_sections,
    section_heading_of_body,
)
from setforge.reconcile.index_model import FileEntry, Index
from setforge.reconcile.merge import merge
from setforge.reconcile.merge_model import Clean, Conflict, MergeResult
from setforge.reconcile.store import (
    prune,
    read_base,
    read_index,
    read_local,
    reconstruct,
    record,
    verify,
    write_base,
    write_index,
    write_local,
)
from setforge.reconcile.types import ABSENT, FileId, HunkClass, file_id

if TYPE_CHECKING:
    from setforge.reconcile.wizard import WizardResult, resolve_conflicts


def __getattr__(name: str) -> Any:  # noqa: ANN401 - lazy public re-export
    if name in {"WizardResult", "resolve_conflicts"}:
        from setforge.reconcile import wizard

        return getattr(wizard, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ABSENT",
    "Clean",
    "Conflict",
    "FileEntry",
    "FileId",
    "HunkClass",
    "Index",
    "MergeResult",
    "WizardResult",
    "file_id",
    "merge",
    "prune",
    "read_base",
    "read_index",
    "read_local",
    "reconstruct",
    "record",
    "record_local_reloc_sections",
    "resolve_conflicts",
    "section_heading_of_body",
    "verify",
    "write_base",
    "write_index",
    "write_local",
]
