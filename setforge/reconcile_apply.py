"""Apply the 3-way reconcile engine to a single plain tracked file.

The install-side glue that activates the (otherwise dormant) reconcile
engine for **plain** tracked files — those with no ``disposition`` and no
``spans`` (files that the legacy path would deploy verbatim). Files that
carry a disposition or spans stay on the legacy ``deploy`` path until
they are migrated onto the engine (a separate follow-up).

:func:`reconcile_plain_file` is pure with respect to the filesystem: it
reads the recorded base from the store, runs :func:`setforge.reconcile.merge`
against the live + tracked content, drives the conflict wizard when needed,
and returns a :class:`ReconcileOutcome` describing what the caller should
do — it never writes the live file and never advances the store itself.
That split keeps the decision logic unit-testable and lets the caller
slot the write + ``record`` into the install pipeline's write pass.

Outcomes encode the A0 guards directly:

- ``NOOP`` — the merge is clean AND already matches live with the base
  already at tracked: an idempotent re-install writes nothing and does not
  re-record (no store churn).
- ``WRITE`` — deploy ``content`` to live and ``record`` ``new_base``.
- ``DEFERRED`` — a region was skipped in the wizard; write nothing and do
  NOT re-baseline (the unresolved upstream change must re-surface next run).
- ``CANCELLED`` — the user aborted the whole-file wizard; write nothing,
  record nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from setforge.reconcile import (
    ABSENT,
    FileId,
    MergeResult,
    merge,
    read_base,
    resolve_conflicts,
)
from setforge.reconcile.merge_model import MergeInput
from setforge.reconcile.types import Absent
from setforge.reconcile.wizard import CANCEL, ClaudeMergeFn, _claude_merge_unavailable

__all__ = ["ReconcileKind", "ReconcileOutcome", "reconcile_plain_file"]


class ReconcileKind(StrEnum):
    """What the caller should do with a :func:`reconcile_plain_file` result."""

    NOOP = "noop"
    WRITE = "write"
    DEFERRED = "deferred"
    CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class ReconcileOutcome:
    """The decision for one plain tracked file.

    ``content`` and ``new_base`` are populated only for :attr:`ReconcileKind.WRITE`:
    ``content`` is the merged bytes to deploy to live (``ABSENT`` ⇒ the file
    should be removed), ``new_base`` is the upstream bytes the caller records
    as the new merge base. Both are ``None`` for the no-write kinds.
    """

    kind: ReconcileKind
    content: bytes | Absent | None = None
    new_base: bytes | None = None


def reconcile_plain_file(
    profile: str,
    fid: FileId,
    *,
    live: bytes | Absent,
    tracked: bytes,
    interactive: bool = False,
    display_path: str | None = None,
    claude_merge: ClaudeMergeFn = _claude_merge_unavailable,
) -> ReconcileOutcome:
    """Decide how to reconcile one plain tracked file via the 3-way engine.

    ``base = read_base`` (``ABSENT`` when no base is recorded — a first
    install or a not-yet-seeded divergence), ``ours = live``, ``theirs =
    tracked``. A clean merge that already equals live with the base already
    at tracked is a :attr:`~ReconcileKind.NOOP`; any other clean merge is a
    :attr:`~ReconcileKind.WRITE` advancing the base to ``tracked``.

    A conflict is resolved interactively ONLY when ``interactive`` is set:
    the per-region wizard (:func:`~setforge.reconcile.resolve_conflicts`)
    runs, and a cancel / deferred (skipped) region writes nothing and does
    NOT re-baseline. When ``interactive`` is False (a non-TTY / ``--auto`` /
    CI install), a conflict is :attr:`~ReconcileKind.DEFERRED` without
    prompting — keeping the local file, leaving the upstream change to
    re-surface on the next interactive run, and letting the caller gate the
    exit code on the deferred count. The full-screen prompt_toolkit wizard
    must never be reached without a TTY.
    """
    base_raw = read_base(profile, fid)
    base: MergeInput = ABSENT if base_raw is None else base_raw
    result: MergeResult = merge(base, live, tracked)

    if result.clean:
        merged = result.merged()
        if merged == live and base_raw == tracked:
            return ReconcileOutcome(ReconcileKind.NOOP)
        return ReconcileOutcome(ReconcileKind.WRITE, content=merged, new_base=tracked)

    if not interactive:
        return ReconcileOutcome(ReconcileKind.DEFERRED)

    wizard = resolve_conflicts(
        fid, result, display_path=display_path, claude_merge=claude_merge
    )
    if wizard is CANCEL:
        return ReconcileOutcome(ReconcileKind.CANCELLED)
    if wizard.deferred:
        return ReconcileOutcome(ReconcileKind.DEFERRED)
    return ReconcileOutcome(
        ReconcileKind.WRITE, content=wizard.merged.merged(), new_base=tracked
    )
