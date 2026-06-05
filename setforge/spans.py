"""Sub-file span intent: the declarative Layer-1 model (no in-file markers).

A *span* freezes (``pinned``) or host-isolates (``forked``) a region of a
tracked file at SUB-file granularity, with the same ``shared|host-local``
semantics keyword the user-section markers carry. Unlike user-sections
the intent is fully stealthy: nothing is written into the file body. The
intent lives declaratively in either ``local.yaml``
(``tracked_files.<id>.spans:``, host-local) or the tracked
``setforge.yaml`` (shared), and the resolved offsets + baseline bytes
live in a sidecar (:mod:`setforge.spans_store`), never duplicated into the
intent (Invariant I12).

This module holds ONLY the intent value object and the closed-set enums,
deliberately free of any merge / capture / relocation logic so both
:mod:`setforge.source` (host-local overlay) and :mod:`setforge.config`
(tracked-side ``TrackedFile``) can import the same shape without a cycle.

Scope note: this wave implements MARKDOWN heading-text anchors only.
Structural dotted-path anchors are representable in the schema
(``anchor`` is a free string) but their *validation* and *resolution*
land in a sibling bead; :func:`validate_spans_file_type` enforces the
markdown-only constraint at install time (wired in
:mod:`setforge.cli._install_helpers`) and leaves the dispatch seam for
the structural case.
"""

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

from setforge.errors import ConfigError

__all__ = [
    "SpanEntry",
    "SpanKind",
    "SpanSemantics",
    "validate_spans_file_type",
]

_STRICT = ConfigDict(extra="forbid")

# Markdown suffixes a heading-text span anchor is permitted on. Mirrors
# :data:`setforge.source._MARKDOWN_SUFFIXES` (kept independent so this
# module imports nothing heavy).
_MARKDOWN_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".markdown"})


class SpanKind(StrEnum):
    """How a span's region is reconciled relative to the whole-file merge.

    ``pinned`` — live wins for this region: an unconditional post-merge
    override re-imposes the live bytes every install, and the region is
    excluded from capture. ``forked`` — the region merges upstream
    normally (no override) but is still excluded from capture. The merge
    difference is the override; the capture exclusion is shared (mirrors
    the file-level :class:`setforge.config.Disposition` FORKED-vs-PINNED
    split).
    """

    PINNED = "pinned"
    FORKED = "forked"


class SpanSemantics(StrEnum):
    """Where a span's intent lives + how tracked-side updates propagate.

    ``host-local`` — intent lives in ``local.yaml``, gitignored,
    per-machine. ``shared`` — intent lives in the tracked ``setforge.yaml``
    and propagates across hosts (tracked-side updates surface in the
    reconcile flow; that surface is a sibling bead). Mirrors
    :class:`setforge.sections.SectionSemantics`.
    """

    HOST_LOCAL = "host-local"
    SHARED = "shared"


class SpanEntry(BaseModel):
    """One declarative span: an anchor plus its kind + semantics.

    ``anchor`` is a markdown heading-text anchor in this wave (e.g.
    ``"## My Tweaks"`` — the ``#`` run encodes the heading level and the
    trailing text is matched byte-exact). Structural dotted-path anchors
    reuse the same free-string field but are validated + resolved in a
    sibling bead. ``kind`` defaults to :data:`SpanKind.PINNED` and
    ``semantics`` to :data:`SpanSemantics.HOST_LOCAL` — the common case
    is a host-local pin; both fields are explicit in the schema so the
    forked / shared siblings are representable today.
    """

    model_config = _STRICT

    anchor: str
    kind: SpanKind = SpanKind.PINNED
    semantics: SpanSemantics = SpanSemantics.HOST_LOCAL

    @field_validator("anchor")
    @classmethod
    def _anchor_non_empty(cls, v: str) -> str:
        """Reject an empty / whitespace-only anchor at parse time."""
        if not v.strip():
            raise ValueError("SpanEntry `anchor` must be non-empty")
        return v


def validate_spans_file_type(
    tracked_file_id: str,
    spans: Sequence[SpanEntry],
    src: Path,
) -> None:
    """Raise :class:`ConfigError` if any span anchor is illegal for ``src``.

    Mirrors :func:`setforge.source.validate_host_local_sections_file_type`:
    a heading-text span anchor is supported only for markdown tracked_files
    (.md / .markdown). Structural dotted-path anchors (for yaml / json)
    are validated in a sibling bead; until then a span on a non-markdown
    file is rejected here so a wrong-file-type anchor surfaces at install
    time (and at ``validate`` time, the offline CI gate), not as a
    confusing runtime relocation failure.

    No-op when ``spans`` is empty — the file may not be markdown but no
    span was declared. The ``src``-suffix dispatch is the seam the
    structural sibling extends (it will route dotted-path anchors to the
    structural validator instead of rejecting them here).
    """
    if not spans:
        return
    suffix = src.suffix.lower()
    if suffix in _MARKDOWN_SUFFIXES:
        return
    raise ConfigError(
        "spans are supported only for markdown tracked_files "
        f"(.md / .markdown) in this release. tracked_file {tracked_file_id!r} "
        f"resolves to src={src} (extension {suffix!r} not in "
        f"{sorted(_MARKDOWN_SUFFIXES)}). Structural dotted-path span anchors "
        "for JSON/YAML are a follow-up."
    )
