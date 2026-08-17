"""Lightweight conflict-choice contracts shared by reconcile callers."""

from __future__ import annotations

from collections.abc import Callable

from setforge.reconcile.merge_model import Conflict
from setforge.ui.primitives import CANCEL, Cancelled

type ClaudeMergeFn = Callable[[Conflict], bytes | Cancelled]


def claude_merge_unavailable(_conflict: Conflict) -> Cancelled:
    """Default Claude-merge stub: decline and let the caller re-prompt."""
    return CANCEL
