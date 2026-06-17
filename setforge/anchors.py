"""Leaf anchor model: the 6-kind markdown splice-point discriminated union.

An :data:`Anchor` names WHERE a host-local body / section is spliced into a
markdown tracked file at install time. The six shapes — ``after-heading``,
``before-heading``, ``at-start-of-file``, ``at-end-of-file``,
``after-section``, ``in-section`` — are matched byte-exact (no slugify /
case-fold) by the inject engine (:mod:`setforge.host_local_inject`).

This module is a LEAF: it imports nothing from setforge beyond the Pydantic
primitives, so both :mod:`setforge.source` (the host-local overlay loader)
and :mod:`setforge.spans` (the OVERLAY span payload) can import the same
:data:`Anchor` union without forming an import cycle. :mod:`setforge.source`
re-exports these names for backward compatibility.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Anchor",
    "AnchorAfterHeading",
    "AnchorAfterSection",
    "AnchorAtEndOfFile",
    "AnchorAtStartOfFile",
    "AnchorBeforeHeading",
    "AnchorInSection",
    "AnchorKind",
]

_STRICT = ConfigDict(extra="forbid")


class AnchorKind(StrEnum):
    """Closed set of anchor-kind discriminator values.

    Six anchor shapes for splicing a host-local body into a markdown
    tracked file at install time. ``after-heading`` / ``before-heading``
    match exact heading text (byte-equal — no case-fold, no
    slug-normalise). ``at-start-of-file`` / ``at-end-of-file`` splice at the
    document boundaries. ``after-section`` references an existing
    user-section in the SAME tracked file by name. ``in-section`` records an
    EXACT position within a heading's section (preceding line + offset) so
    host-local content re-lands where it was typed, not just under the heading.
    """

    AFTER_HEADING = "after-heading"
    BEFORE_HEADING = "before-heading"
    AT_START_OF_FILE = "at-start-of-file"
    AT_END_OF_FILE = "at-end-of-file"
    AFTER_SECTION = "after-section"
    IN_SECTION = "in-section"


class AnchorAfterHeading(BaseModel):
    """Anchor matching the line immediately following the heading ``value``."""

    model_config = _STRICT

    kind: Literal[AnchorKind.AFTER_HEADING] = AnchorKind.AFTER_HEADING
    value: str


class AnchorBeforeHeading(BaseModel):
    """Anchor matching the line immediately preceding the heading ``value``."""

    model_config = _STRICT

    kind: Literal[AnchorKind.BEFORE_HEADING] = AnchorKind.BEFORE_HEADING
    value: str


class AnchorAtStartOfFile(BaseModel):
    """Anchor matching the first line of the file (line offset 0)."""

    model_config = _STRICT

    kind: Literal[AnchorKind.AT_START_OF_FILE] = AnchorKind.AT_START_OF_FILE


class AnchorAtEndOfFile(BaseModel):
    """Anchor matching the line after the last line of the file."""

    model_config = _STRICT

    kind: Literal[AnchorKind.AT_END_OF_FILE] = AnchorKind.AT_END_OF_FILE


class AnchorAfterSection(BaseModel):
    """Anchor matching the line after the end marker of section ``name``."""

    model_config = _STRICT

    kind: Literal[AnchorKind.AFTER_SECTION] = AnchorKind.AFTER_SECTION
    name: str


class AnchorInSection(BaseModel):
    """Anchor for host-local content typed at a specific position WITHIN a
    section, below the section's first content line.

    Re-lands the body exactly where it was hand-typed rather than just under
    the enclosing heading. ``heading`` / ``level`` identify the section (the
    section spans from the heading to the next heading of level <= ``level``);
    ``after_line`` is the nearest preceding NON-BLANK content line (LF-stripped)
    and is the primary, text-stable splice point; ``offset`` (lines below the
    heading) is the secondary resolver used when ``after_line`` is absent or no
    longer uniquely present. When BOTH fail but the heading still resolves, the body
    degrades to the end of the section (the resolver signals this so deploy can
    warn). See :func:`setforge.host_local_inject._resolve_in_section`.
    """

    model_config = _STRICT

    kind: Literal[AnchorKind.IN_SECTION] = AnchorKind.IN_SECTION
    heading: str
    level: int
    after_line: str | None
    offset: int


Anchor = Annotated[
    AnchorAfterHeading
    | AnchorBeforeHeading
    | AnchorAtStartOfFile
    | AnchorAtEndOfFile
    | AnchorAfterSection
    | AnchorInSection,
    Field(discriminator="kind"),
]
