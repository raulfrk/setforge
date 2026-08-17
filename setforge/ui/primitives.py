"""Lightweight value types shared by interactive UI surfaces.

This module deliberately has no prompt-toolkit imports. Non-interactive CLI
startup can therefore type and compare widget choices without loading the
terminal application stack until a prompt is actually opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Final


class _Cancelled(Enum):
    """The type of the :data:`CANCEL` singleton."""

    TOKEN = auto()


CANCEL: Final = _Cancelled.TOKEN
"""Sentinel returned when an interactive UI is cancelled."""

type Cancelled = _Cancelled


@dataclass(frozen=True, slots=True)
class Button[T]:
    """One selectable button and the value it yields."""

    label: str
    value: T
    key: str | None = None
