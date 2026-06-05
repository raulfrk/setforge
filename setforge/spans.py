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

Scope note: this module supports BOTH markdown heading-text anchors and
structural (yaml/json/jsonc) dotted-path anchors. The legal anchor grammar
is file-type-dispatched at install / validate time by
:func:`validate_spans_file_type` (markdown → heading anchor only; structural
→ dotted-path anchor only), so an ``anchor: str`` that is shaped for the
wrong file type is rejected up front rather than failing as a confusing
runtime relocation error. Resolution of structural dotted paths lives in
:mod:`setforge.disposition_merge` (the 3-way merge re-assert) and
:mod:`setforge.structural_merge` (:func:`~setforge.structural_merge.set_at_path`
/ :func:`~setforge.structural_merge.get_at_path`).
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
    "is_heading_anchor",
    "validate_spans_file_type",
]

_STRICT = ConfigDict(extra="forbid")

# Markdown suffixes a heading-text span anchor is permitted on. Mirrors
# :data:`setforge.source._MARKDOWN_SUFFIXES` (kept independent so this
# module imports nothing heavy).
_MARKDOWN_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".markdown"})

# Structural (comment-preserving tree) suffixes a dotted-path span anchor is
# permitted on. Mirrors the dispatch in
# :func:`setforge.disposition_merge._is_structural` (kept independent so this
# leaf module imports nothing heavy).
_STRUCTURAL_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".yaml", ".yml", ".json", ".jsonc"}
)


def is_heading_anchor(anchor: str) -> bool:
    """Whether ``anchor`` is markdown-heading-shaped (leading ``#`` run).

    A markdown span anchor encodes the heading level via its ``#`` run; a
    structural dotted-path anchor (``a.b.c``) never starts with ``#``. The
    classification is the file-type dispatch's discriminator: heading-shaped
    anchors are legal only on markdown, dotted-path anchors only on structural
    files.
    """
    return anchor.lstrip().startswith("#")


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

    ``anchor`` is EITHER a markdown heading-text anchor (e.g. ``"## My
    Tweaks"`` — the ``#`` run encodes the heading level and the trailing text
    is matched byte-exact) OR a structural dotted path (e.g.
    ``"editor.fontSize"`` — a mapping leaf or whole-subtree in the
    :func:`~setforge.structural_merge.set_at_path` grammar). Which grammar is
    legal is file-type-dispatched by :func:`validate_spans_file_type`. ``kind``
    defaults to :data:`SpanKind.PINNED` and
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

    File-type-dispatched anchor-grammar validation (mirrors
    :func:`setforge.source.validate_host_local_sections_file_type`):

    * markdown (``.md`` / ``.markdown``): a span anchor must be heading-shaped
      (a leading ``#`` run, per :func:`is_heading_anchor`). A dotted-path anchor
      on markdown is rejected.
    * structural (``.yaml`` / ``.yml`` / ``.json`` / ``.jsonc``): a span anchor
      must be a dotted path (NOT heading-shaped). A heading anchor on a
      structural file is rejected.
    * any other suffix: spans are unsupported entirely.

    Resolving the grammar at parse / validate time (no runtime
    ``--heading/--structural`` flag) means a wrong-file-type anchor surfaces at
    install time AND at ``validate`` time (the offline CI gate), not as a
    confusing runtime relocation failure.

    No-op when ``spans`` is empty — the file's type is irrelevant if nothing was
    declared.
    """
    if not spans:
        return
    suffix = src.suffix.lower()
    if suffix in _MARKDOWN_SUFFIXES:
        _validate_anchors_for_markdown(tracked_file_id, spans, src)
        return
    if suffix in _STRUCTURAL_SUFFIXES:
        _validate_anchors_for_structural(tracked_file_id, spans, src)
        return
    raise ConfigError(
        "spans are supported only for markdown (.md / .markdown) and structural "
        f"(.yaml / .yml / .json / .jsonc) tracked_files. tracked_file "
        f"{tracked_file_id!r} resolves to src={src} (extension {suffix!r})."
    )


def _validate_anchors_for_markdown(
    tracked_file_id: str, spans: Sequence[SpanEntry], src: Path
) -> None:
    """Reject any non-heading-shaped anchor on a markdown ``src``."""
    for span in spans:
        if not is_heading_anchor(span.anchor):
            raise ConfigError(
                f"tracked_file {tracked_file_id!r} (src={src}) is markdown, so "
                f"span anchors must be heading-shaped (a leading '#' run), but "
                f"anchor {span.anchor!r} is not. Dotted-path anchors are for "
                "structural (yaml/json/jsonc) files."
            )


def _validate_anchors_for_structural(
    tracked_file_id: str, spans: Sequence[SpanEntry], src: Path
) -> None:
    """Reject any heading-shaped anchor on a structural ``src``."""
    for span in spans:
        if is_heading_anchor(span.anchor):
            raise ConfigError(
                f"tracked_file {tracked_file_id!r} (src={src}) is structural "
                f"(yaml/json/jsonc), so span anchors must be dotted paths, but "
                f"anchor {span.anchor!r} is heading-shaped. Heading anchors are "
                "for markdown files."
            )
