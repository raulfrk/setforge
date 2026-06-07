"""Deploy-side helper for markerless OVERLAY spans.

The OVERLAY deploy contract (the heart of the leak gate):

1. **Pre-merge excise** (:func:`excise_overlay_bodies`): strip every
   overlay body from the live file BEFORE the 3-way merge, by the exact
   recorded bytes (canonical body plus ``last_deployed_body``). The merge then
   never sees the body, so it cannot reflow / conflict-fold against
   adjacent shared edits and cannot leak the body into the re-baselined
   base.
2. **Post-merge inject** (:func:`inject_overlay_bodies`): re-impose each
   canonical body at its anchor AFTER the whole-file merge (and after any
   pinned/forked span re-overlay). The body is ``local.yaml``-authoritative
   — re-imposed every deploy. Multi-section injects run bottom-up by anchor
   line so an earlier splice never invalidates a later anchor's offset.

The body is naked text (no markers). The base is re-baselined from the
PRE-inject bytes by the caller, so the stored base is always body-free.
First deploy (no recorded body, none present in live) injects
unconditionally — never treats "the body already appears once as shared
content" as "already deployed".
"""

from __future__ import annotations

import hashlib

from setforge.overlay_inject import (
    canonical_body,
    excise_unique_needle,
    inject_body_at_anchor,
)
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind
from setforge.spans_store import SpanState

__all__ = [
    "canonical_overlay_body",
    "excise_overlay_bodies",
    "inject_overlay_bodies",
    "overlay_spans",
]

_CONTEXT_LINES = 3


def overlay_spans(spans: list[SpanEntry]) -> list[SpanEntry]:
    """Return only the OVERLAY-kind spans in ``spans`` (declaration order)."""
    return [s for s in spans if s.kind is SpanKind.OVERLAY]


def canonical_overlay_body(payload: OverlaySpanPayload) -> str:
    """Return the canonical body bytes of an overlay payload.

    Reads the inline ``body`` or the ``body_file`` (validated non-empty at
    read time, mirroring :func:`setforge.host_local_inject._read_body`) and
    canonicalises to LF + single trailing newline. The returned string is
    the body's identity for both injection and excision.
    """
    if payload.body is not None:
        return canonical_body(payload.body)
    assert payload.body_file is not None  # exactly-one-of guarantee
    raw = payload.body_file.read_text(encoding="utf-8")
    if not raw.strip():
        raise ValueError(f"OverlaySpanPayload `body_file` {payload.body_file} is empty")
    return canonical_body(raw)


def _needles(span: SpanEntry, stored: SpanState | None) -> list[str]:
    """Ordered excise needle set for ``span``: last-deployed first, then canonical.

    Preferring ``last_deployed_body`` first means a body the user changed in
    ``local.yaml`` between deploys still excises cleanly (live carries the
    OLD body). The canonical (current) body is the fallback for the
    same-body steady state and the very first capture.
    """
    assert span.overlay is not None
    needles: list[str] = []
    if stored is not None and stored.last_deployed_body:
        needles.append(stored.last_deployed_body)
    canonical = canonical_overlay_body(span.overlay)
    if canonical not in needles:
        needles.append(canonical)
    return needles


def excise_overlay_bodies(
    live_text: str,
    spans: list[SpanEntry],
    stored_states: dict[str, SpanState],
) -> tuple[str, bool]:
    """Strip every overlay body from ``live_text`` by its exact recorded bytes.

    For each OVERLAY span, excise the unique occurrence of a needle (see
    :func:`_needles`). Returns ``(body_free_text, found_any)``. A needle
    that occurs more than once raises
    :class:`~setforge.overlay_inject.OverlayAmbiguousError` (REFUSE — never
    guess which occurrence is the body). A needle absent from ``live_text``
    is the first-deploy / fully-hand-edited case: that span contributes no
    excise and ``found_any`` reflects whether ANY overlay body was removed.

    The result is the body-free text the 3-way merge consumes.

    Transitively raises (via :func:`canonical_overlay_body` when a span's
    body is a ``body_file``): :class:`ValueError` when the ``body_file`` is
    empty, and :class:`OSError` / :class:`FileNotFoundError` when it cannot
    be read.
    """
    text = live_text
    found_any = False
    for span in overlay_spans(spans):
        stored = stored_states.get(span.anchor)
        text, found = excise_unique_needle(text, _needles(span, stored))
        if found is not None:
            found_any = True
    return text, found_any


def _state_from_injection(
    anchor: str, text: str, start_line: int, end_line: int, body: str
) -> SpanState:
    """Build a sidecar record for an injected overlay region.

    The relocation fields (fingerprint / prefix / suffix / hints) describe
    the injected body region so the capture-side wizard's fuzzy locate has
    derived state. ``last_deployed_body`` records the EXACT canonical bytes
    injected — the excise needle for the next deploy / capture.
    """
    lines = text.splitlines()
    return SpanState(
        anchor=anchor,
        fingerprint=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        prefix=lines[max(0, start_line - _CONTEXT_LINES) : start_line],
        suffix=lines[end_line : end_line + _CONTEXT_LINES],
        position_hint_start_line=start_line,
        position_hint_n_lines=end_line - start_line,
        heading_level=_heading_level_for(anchor),
        last_deployed_body=body,
    )


def _heading_level_for(anchor: str) -> int:
    """Return the ATX level of an overlay span's heading-identity anchor (1 default)."""
    stripped = anchor.lstrip()
    run = len(stripped) - len(stripped.lstrip("#"))
    return run if 1 <= run <= 6 else 1


def inject_overlay_bodies(
    merged_text: str,
    spans: list[SpanEntry],
    stored_states: dict[str, SpanState],
) -> tuple[str, dict[str, SpanState]]:
    """Inject every overlay body into ``merged_text`` at its anchor.

    Runs AFTER the whole-file merge (and after pinned/forked re-overlay) on
    body-free text. Each canonical body is spliced at its
    :attr:`~setforge.spans.OverlaySpanPayload.anchor`; multi-section injects
    apply bottom-up by the resolved anchor line so an earlier splice never
    shifts a later anchor. Returns ``(injected_text, new_states)`` where
    ``new_states`` maps each overlay anchor to its recomputed
    :class:`~setforge.spans_store.SpanState` (carrying the new
    ``last_deployed_body``).

    Unconditional first-deploy inject: ``stored_states`` is consulted only
    to carry forward context, never to skip an inject — a body that happens
    to already appear once as shared content is STILL injected (the merge
    consumed body-free text, so any pre-existing occurrence is genuinely
    shared, not the overlay).

    Raises the anchor resolution errors of
    :func:`~setforge.overlay_inject.inject_body_at_anchor` (anchor missing /
    ambiguous) BEFORE returning. Also transitively raises (via
    :func:`canonical_overlay_body` when a span's body is a ``body_file``):
    :class:`ValueError` when the ``body_file`` is empty, and
    :class:`OSError` / :class:`FileNotFoundError` when it cannot be read.
    """
    del stored_states  # consulted by the caller for excise, not for inject
    ov = overlay_spans(spans)
    if not ov:
        return merged_text, {}

    # Resolve every anchor line on the FROZEN pre-inject text, then apply
    # bottom-up so an earlier splice never invalidates a later offset.
    bodies: dict[str, str] = {}
    ordered: list[tuple[int, SpanEntry]] = []
    for span in ov:
        assert span.overlay is not None
        body = canonical_overlay_body(span.overlay)
        bodies[span.anchor] = body
        line = _resolve_anchor_line(merged_text, span)
        ordered.append((line, span))

    text = merged_text
    for _line, span in sorted(ordered, key=lambda t: t[0], reverse=True):
        assert span.overlay is not None
        text = inject_body_at_anchor(text, span.overlay.anchor, bodies[span.anchor])

    # Recompute each span's state from the FINAL injected text. Locate each
    # body AT/AFTER its resolved anchor line on the final text so a body that
    # also appears as shared content ABOVE the anchor does not mislocate the
    # region (and its prefix/suffix hints).
    new_states: dict[str, SpanState] = {}
    for span in ov:
        body = bodies[span.anchor]
        anchor_line = _resolve_anchor_line(text, span)
        start, end = _locate_injected_body(text, body, anchor_line)
        new_states[span.anchor] = _state_from_injection(
            span.anchor, text, start, end, body
        )
    return text, new_states


def _resolve_anchor_line(text: str, span: SpanEntry) -> int:
    """Return the resolved injection line offset of an overlay span's anchor."""
    from setforge.host_local_inject import _normalise_eol, _resolve_anchor_lf

    assert span.overlay is not None
    return _resolve_anchor_lf(_normalise_eol(text), span.overlay.anchor)


def _locate_injected_body(text: str, body: str, anchor_line: int) -> tuple[int, int]:
    """Return the ``[start_line, end_line)`` of ``body`` injected at ``anchor_line``.

    The body was just spliced in by :func:`inject_body_at_anchor` immediately
    after ``anchor_line``, so search for it AT/AFTER that line's char offset
    rather than from the start of ``text``. A plain ``text.index(body)`` would
    record the WRONG region's prefix/suffix hints when the same body text also
    appears as shared content above the anchor; anchoring the search to the
    injection point pins the correct occurrence. The body is present (just
    spliced), so the search succeeds.
    """
    lines = text.splitlines(keepends=True)
    search_from = len("".join(lines[:anchor_line]))
    idx = text.index(body, search_from)
    start_line = text.count("\n", 0, idx)
    n_lines = body.count("\n")
    return start_line, start_line + n_lines
