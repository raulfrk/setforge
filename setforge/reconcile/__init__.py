"""Reconcile engine: the base/local/index store substrate (A1).

Stable public façade — call sites import ``from setforge.reconcile import ...``;
the submodules (:mod:`~setforge.reconcile.store`, ``index_model``, ``types``) are
internal and may be refactored without touching consumers.
"""

from __future__ import annotations

from setforge.reconcile.index_model import FileEntry, Index
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

__all__ = [
    "ABSENT",
    "FileEntry",
    "FileId",
    "HunkClass",
    "Index",
    "file_id",
    "prune",
    "read_base",
    "read_index",
    "read_local",
    "reconstruct",
    "record",
    "verify",
    "write_base",
    "write_index",
    "write_local",
]
