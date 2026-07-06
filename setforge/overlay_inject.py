"""Compatibility shim: the overlay body primitives moved to :mod:`setforge.body_canon`.

The leak-safe inject / excise primitives (``canonical_body``,
``inject_body_at_anchor``, ``excise_unique_needle``, ``OverlayAmbiguousError``)
now live in the reconcile-native :mod:`setforge.body_canon`. This module remains
a thin re-export so the frozen migration consumers that still import
``setforge.overlay_inject`` keep working until they are retired. New code must
import from :mod:`setforge.body_canon` directly.
"""

from __future__ import annotations

from setforge.body_canon import (
    OverlayAmbiguousError,
    canonical_body,
    excise_unique_needle,
    inject_body_at_anchor,
)

__all__ = [
    "OverlayAmbiguousError",
    "canonical_body",
    "excise_unique_needle",
    "inject_body_at_anchor",
]
