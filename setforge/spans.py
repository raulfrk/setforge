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
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from setforge.anchors import Anchor
from setforge.errors import ConfigError
from setforge.section_mode import SectionMode

if TYPE_CHECKING:
    # Type-only: importing Disposition at runtime would cycle
    # (setforge.config imports SpanEntry from this module).
    from setforge.config import Disposition

__all__ = [
    "OverlaySpanPayload",
    "SpanEntry",
    "SpanKind",
    "SpanSemantics",
    "is_heading_anchor",
    "validate_span_disposition",
    "validate_spans_file_type",
]

_STRICT = ConfigDict(extra="forbid")

# Markdown suffixes a heading-text span anchor is permitted on. Mirrors
# :data:`setforge.source._MARKDOWN_SUFFIXES` (kept independent so this
# module imports nothing heavy).
_MARKDOWN_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".markdown"})

# Structural (comment-preserving tree) suffixes a dotted-path span anchor is
# permitted on. Mirrors the dispatch in
# :func:`setforge.disposition_merge.is_structural` (kept independent so this
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

    ``overlay`` — a markerless host-local body that NEVER enters tracked
    content, the span region, or the 3-way merge. The body lives in
    ``local.yaml`` (the :attr:`SpanEntry.overlay` payload); deploy injects
    it AFTER the merge and excises it BEFORE the merge, and capture excises
    the exact body bytes before any tracked write (see
    :mod:`setforge.overlay_inject`). Markdown-only.
    """

    PINNED = "pinned"
    FORKED = "forked"
    OVERLAY = "overlay"


class SpanSemantics(StrEnum):
    """Where a span's intent lives + how tracked-side updates propagate.

    ``host-local`` — intent lives in ``local.yaml``, gitignored,
    per-machine. ``shared`` — intent lives in the tracked ``setforge.yaml``
    and propagates across hosts (tracked-side updates surface in the
    reconcile flow). Mirrors
    :class:`setforge.sections.SectionSemantics`.
    """

    HOST_LOCAL = "host-local"
    SHARED = "shared"


class OverlaySpanPayload(BaseModel):
    """The host-local body payload of an OVERLAY span.

    Carries a structured :data:`~setforge.anchors.Anchor` (the splice point
    the body is injected at, e.g. ``after-heading "Notes"``) and exactly one
    of ``body`` (inline string) / ``body_file`` (path read at install time).
    Lives only in ``local.yaml``; the body NEVER enters tracked content
    (markerless OVERLAY). Both / neither body source is a configuration
    error surfaced at :class:`pydantic.ValidationError` time.

    The exactly-one-of + non-empty-inline checks mirror
    :class:`setforge.source.HostLocalSection`; the FS-touching empty
    ``body_file`` check is deferred to the read site (so schema parsing
    stays decoupled from live FS state).
    """

    model_config = _STRICT

    anchor: Anchor
    body: str | None = None
    body_file: Path | None = None

    @model_validator(mode="after")
    def _exactly_one_body_source(self) -> "OverlaySpanPayload":
        """Enforce exactly-one-of ``body`` / ``body_file`` + non-empty inline."""
        if (self.body is None) == (self.body_file is None):
            shape = "both" if self.body is not None else "neither"
            raise ValueError(
                "OverlaySpanPayload requires exactly one of `body` (inline) "
                f"or `body_file` (path); got {shape}"
            )
        if self.body is not None and not self.body.strip():
            raise ValueError("OverlaySpanPayload `body` must be non-empty")
        return self


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

    ``overlay`` is the markerless host-local body payload, present iff
    ``kind == overlay`` (the model validator enforces the biconditional).
    For an OVERLAY span ``anchor`` is the span's stable IDENTITY (the
    anchor-keyed sidecar key); ``overlay.anchor`` is the structured splice
    point. The two coincide for an after-heading overlay but the identity
    string is what keys ``last_deployed_body`` in the sidecar.
    """

    model_config = _STRICT

    anchor: str
    kind: SpanKind = SpanKind.PINNED
    semantics: SpanSemantics = SpanSemantics.HOST_LOCAL
    overlay: OverlaySpanPayload | None = None
    deep: bool = False
    """Deep-recursive re-assert for a PINNED structural span (schema 2.0).

    ``False`` (default) → the PINNED re-assert whole-replaces the value at
    the anchor with live's. ``True`` → the re-assert DEEP-merges live over
    the merged value: tracked-only sub-keys survive, live-only sub-keys are
    added, shared scalars take live's value (carries the legacy
    ``preserve_user_keys_deep`` semantics). Legal only on a structural
    (dotted-path) PINNED / FORKED span — the model validator rejects it on
    an OVERLAY span or a markdown heading anchor, where deep-merge has no
    meaning.
    """
    capture_mode: SectionMode = SectionMode.KEEP_DEFAULTS
    """Provenance-only carrier for the legacy ``preserve_user_sections_mode``.

    INERT AT SCHEMA 2.0 — it has no runtime consumer. The shared-section
    capture path excludes the whole span region wholesale regardless of
    mode, so ``KEEP_DEFAULTS`` and ``STRIP`` capture identically; the field
    exists solely to round-trip the legacy ``preserve_user_sections_mode``
    flag through the 1.2 ↔ 2.0 migration (it is frozen in
    ``FROZEN_FIELD_MANIFEST`` and restored by the reverse migration). It is
    accept-and-ignored on every span kind (no validation error). If section
    capture ever grows a per-mode behavior, this is the field to wire.
    """

    @field_validator("anchor")
    @classmethod
    def _anchor_non_empty(cls, v: str) -> str:
        """Reject an empty / whitespace-only anchor at parse time."""
        if not v.strip():
            raise ValueError("SpanEntry `anchor` must be non-empty")
        return v

    @model_validator(mode="after")
    def _overlay_payload_iff_overlay_kind(self) -> "SpanEntry":
        """Enforce the ``overlay`` payload is present iff ``kind == overlay``."""
        if self.kind is SpanKind.OVERLAY and self.overlay is None:
            raise ValueError(
                "SpanEntry with kind=overlay requires an `overlay` body payload"
            )
        if self.kind is not SpanKind.OVERLAY and self.overlay is not None:
            raise ValueError(
                f"SpanEntry with kind={self.kind.value} must not carry an "
                "`overlay` payload (the payload is for kind=overlay only)"
            )
        return self

    @model_validator(mode="after")
    def _deep_only_on_structural_span(self) -> "SpanEntry":
        """Reject ``deep=True`` on an OVERLAY span or a markdown heading anchor.

        Deep-merge re-assert applies to a structural (dotted-path) subtree
        only: an OVERLAY span has no merge re-assert at all, and a markdown
        heading anchor addresses a line-based body, not a mapping. ``deep``
        is therefore illegal on both. ``capture_mode`` carries no such
        constraint — it is accept-and-ignored on non-section spans.
        """
        if not self.deep:
            return self
        if self.kind is SpanKind.OVERLAY:
            raise ValueError(
                "SpanEntry deep=True is invalid on kind=overlay (an OVERLAY "
                "span has no merge re-assert to deep-merge)"
            )
        if is_heading_anchor(self.anchor):
            raise ValueError(
                f"SpanEntry deep=True is invalid on the markdown heading "
                f"anchor {self.anchor!r}; deep-merge applies to structural "
                "(dotted-path) span anchors only"
            )
        return self


def validate_span_disposition(
    tracked_file_id: str,
    spans: Sequence[SpanEntry],
    disposition: "Disposition | None",
) -> None:
    """Raise :class:`ConfigError` if a PINNED/FORKED span has no disposition.

    A ``pinned``/``forked`` span is consumed only on the disposition merge
    path (:func:`setforge.disposition_merge.resolve_file` → span re-overlay).
    On a ``disposition: None`` file the verbatim deploy
    (:func:`setforge.deploy._verbatim_with_overlay`) processes ONLY ``overlay``
    spans, so a pinned/forked span is silently ignored on deploy AND its region
    is never excluded from capture — host-local content can then leak into
    tracked under ``sync --auto=use-live``.

    ``overlay`` spans are EXEMPT: they are the markerless host-local-body
    mechanism and run on the ``disposition=None`` path. Any disposition
    (``shared``/``forked``/``pinned``) routes the file through the merge path
    where the span IS honored, so only a ``None`` disposition is rejected. Keys
    strictly on ``disposition`` (the file's merge policy), never on a span's
    ``semantics`` (an orthogonal axis: where the intent is declared).

    Raises on the FIRST offending span (mirrors
    :func:`validate_spans_file_type`); the caller annotates with the
    tracked_file context.
    """
    if disposition is not None:
        return
    for span in spans:
        if span.kind is SpanKind.OVERLAY:
            continue
        raise ConfigError(
            f"tracked_file {tracked_file_id!r} declares a {span.kind.value!r} "
            f"span (anchor {span.anchor!r}) but its disposition is None, so the "
            "span is silently ignored on deploy and not excluded on capture "
            "(host-local content can leak into tracked). Fix: set "
            "'disposition: shared|forked|pinned' on the tracked_file, or use "
            "'kind: overlay' for a host-local body that needs no disposition."
        )


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
    """Reject any non-heading-shaped anchor on a markdown ``src``.

    OVERLAY spans are EXEMPT from the heading-shape requirement: an
    OVERLAY span's top-level ``anchor`` is the span's sidecar IDENTITY
    (an anchor-keyed record key), NOT the splice point — the actual
    splice point is the structured :attr:`OverlaySpanPayload.anchor`
    (validated as a discriminated union at parse time). The identity may
    therefore be any non-empty unique string (e.g. the retired
    ``host_local_sections.<name>`` key the migration carries over). For
    PINNED / FORKED spans the ``anchor`` IS the heading-text splice point,
    so the heading-shape constraint still applies to them.
    """
    for span in spans:
        if span.kind is SpanKind.OVERLAY:
            continue
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
    """Reject any heading-shaped anchor + any OVERLAY span on a structural ``src``."""
    for span in spans:
        if span.kind is SpanKind.OVERLAY:
            raise ConfigError(
                f"tracked_file {tracked_file_id!r} (src={src}) is structural "
                f"(yaml/json/jsonc), but span anchor {span.anchor!r} declares "
                "kind=overlay. Markerless OVERLAY (host-local body) spans are "
                "markdown-only; structural files have no heading to splice a "
                "naked body below."
            )
        if is_heading_anchor(span.anchor):
            raise ConfigError(
                f"tracked_file {tracked_file_id!r} (src={src}) is structural "
                f"(yaml/json/jsonc), so span anchors must be dotted paths, but "
                f"anchor {span.anchor!r} is heading-shaped. Heading anchors are "
                "for markdown files."
            )
