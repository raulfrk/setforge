"""Canonical form and anchor injection for a host-local section body.

A body is canonicalised to LF with exactly one trailing newline by
:func:`canonical_body` before it is injected, so a CRLF live file and an LF
tracked file agree on its exact bytes. :func:`inject_body_at_anchor` splices
the canonical body at a resolved anchor as naked text: no markers appear in
the deployed file. Both functions are pure over text.
"""

from __future__ import annotations

from setforge.host_local_inject import _normalise_eol, _resolve_anchor_lf
from setforge.source import Anchor

__all__ = [
    "canonical_body",
    "inject_body_at_anchor",
]


def canonical_body(body: str) -> str:
    """Return ``body`` EOL-normalised to LF with exactly one trailing ``\\n``.

    The canonical form is the body's identity for both injection (the
    payload spliced in) and excision (the needle searched for). Collapsing
    CRLF/CR to LF and pinning a single trailing newline (whole-line) makes
    the needle stable across the CRLF-live / LF-tracked split and the
    deploy head-``\\n`` fixup. An all-whitespace body canonicalises to a
    single ``\\n``; callers validate non-emptiness upstream.
    """
    normalised = _normalise_eol(body)
    return normalised.rstrip("\n") + "\n"


def inject_body_at_anchor(text: str, anchor: Anchor, body: str) -> str:
    """Splice ``body`` into ``text`` at ``anchor``'s resolved line offset.

    ``body`` MUST already be canonical (:func:`canonical_body`). ``text`` is
    EOL-normalised at the splice boundary so a CRLF live file matches the
    same headings as the LF tracked source. The body is spliced verbatim —
    no markers, no hash stamping (markerless OVERLAY). Reuses the
    host-local inject engine's anchor resolver + head/tail keepends logic.

    Raises :class:`~setforge.errors.AnchorNotFoundError` /
    :class:`~setforge.errors.AnchorAmbiguousError` (both
    :class:`~setforge.errors.ConfigError`) before returning when the anchor
    matches zero / multiple candidates.
    """
    normalised = _normalise_eol(text)
    line_offset = _resolve_anchor_lf(normalised, anchor)
    lines = normalised.splitlines(keepends=True)
    head = "".join(lines[:line_offset])
    tail = "".join(lines[line_offset:])
    if head and not head.endswith("\n"):
        head += "\n"
    return head + body + tail
