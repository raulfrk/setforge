"""Reconcile engine: the base/local/index store + the line-level 3-way merge.

Stable public façade — call sites import ``from setforge.reconcile import ...``;
the submodules (:mod:`~setforge.reconcile.store`, ``index_model``, ``merge``,
``merge_model``, ``types``, ``wizard``) are internal and may be refactored
without touching consumers.
"""

from __future__ import annotations

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
from setforge.reconcile.wizard import WizardResult, resolve_conflicts

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
    "resolve_conflicts",
    "verify",
    "write_base",
    "write_index",
    "write_local",
]
