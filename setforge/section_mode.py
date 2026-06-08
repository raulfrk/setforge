"""The :class:`SectionMode` closed set — a leaf module with no heavy imports.

``SectionMode`` lives here (rather than in :mod:`setforge.config`) so
:mod:`setforge.spans` can carry it on :attr:`~setforge.spans.SpanEntry.capture_mode`
WITHOUT the import cycle a ``spans → config → spans`` edge would create
(:mod:`setforge.config` already imports :class:`~setforge.spans.SpanEntry`).
:mod:`setforge.config` re-exports the name for back-compat, so existing
``from setforge.config import SectionMode`` call sites stay valid.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SectionMode"]


class SectionMode(StrEnum):
    """How capture treats marker bodies in section-preserving tracked files.

    ``keep_defaults`` (default, non-destructive): capture re-splices the
    tracked file's existing marker bodies into the live content before
    writing tracked, so global defaults baked into tracked survive every
    sync. Falls back to ``strip`` semantics when there's no existing
    tracked file (no defaults to preserve).

    ``strip`` (opt-in, destructive): capture wipes marker bodies entirely.
    Use only when markers are pure host-local placeholders that must
    never persist into the tracked source.

    Historically this drove the legacy ``preserve_user_sections_mode``
    field; at schema 2.0 it is carried by
    :attr:`setforge.spans.SpanEntry.capture_mode` on section spans.
    """

    KEEP_DEFAULTS = "keep_defaults"
    STRIP = "strip"
