"""Leak-safe inject / excise primitives for markerless OVERLAY spans.

The OVERLAY span model keeps a host-local body OUT of tracked content, the
span region, and the 3-way merge. The body lives in ``local.yaml``; deploy
injects it AFTER the whole-file merge and excises it BEFORE the merge, and
capture excises the exact body bytes before any tracked write. No markers
appear in the deployed file — the body is naked text.

The load-bearing invariant of this whole bead lives here: the body's
identity is the **exact recorded body BYTES** (the needle set), never a
re-derived anchor / structure / offset. Inject splices the canonical body
at a resolved anchor; excise removes the *unique* occurrence of a known
needle with a plain length-exact splice (NO seam-collapse ``\\n``-drop).
Both functions are pure over text so the deploy / capture seams and the
canonical leak-gate test rest on them directly.

EOL handling: a body is canonicalised to LF with exactly one trailing
newline by :func:`canonical_body` before it is ever used as a needle or an
injected payload, so a CRLF live file and an LF tracked file agree on the
exact bytes to find / remove (anti-smell: byte-offset framing under the
deploy head-``\\n`` fixup + CRLF).
"""

from __future__ import annotations

from collections.abc import Sequence

from setforge.errors import SetforgeError
from setforge.host_local_inject import _normalise_eol, _resolve_anchor_lf
from setforge.source import Anchor

__all__ = [
    "OverlayAmbiguousError",
    "canonical_body",
    "excise_unique_needle",
    "inject_body_at_anchor",
]


class OverlayAmbiguousError(SetforgeError):
    """A needle occurs more than once in the target text.

    Raised by :func:`excise_unique_needle` when a body needle the caller
    asked to excise appears at >1 location. The caller must REFUSE rather
    than guess which occurrence is the host-local body — guessing risks
    either a leak (excise the wrong one, the body survives into tracked) or
    a corruption (excise shared content).
    """


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


def excise_unique_needle(text: str, needles: Sequence[str]) -> tuple[str, str | None]:
    """Remove the first uniquely-occurring needle from ``text``.

    ``needles`` is tried in order (the caller puts the most-likely needle —
    e.g. ``last_deployed_body`` — first). For each needle, count its exact
    occurrences in EOL-normalised ``text``:

    * exactly one → splice it out with a plain length-exact removal
      (``text[:i] + text[i + len(needle):]``); return ``(excised, needle)``.
    * more than one → raise :class:`OverlayAmbiguousError` (REFUSE — never
      guess which occurrence is the body).
    * zero → try the next needle.

    Returns ``(text_normalised, None)`` when NO needle occurs (the
    first-deploy / fully-hand-edited case the caller handles separately).

    The splice is byte-exact: NO seam-collapse ``\\n``-drop heuristic. An
    after-heading body that landed as ``…\\nBODY\\n…`` is removed leaving the
    surrounding newlines intact, so the round-trip with
    :func:`inject_body_at_anchor` is exact.
    """
    normalised = _normalise_eol(text)
    for needle in needles:
        if not needle:
            continue
        count = normalised.count(needle)
        if count == 0:
            continue
        if count > 1:
            raise OverlayAmbiguousError(
                f"overlay body occurs {count} times in the target text; "
                "refusing to guess which occurrence is the host-local body"
            )
        idx = normalised.index(needle)
        return normalised[:idx] + normalised[idx + len(needle) :], needle
    return normalised, None
