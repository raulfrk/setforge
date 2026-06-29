"""The marker-retire migration — schema 2.0 → 2.1.

Retires the user-section MARKER mechanism: strips every ``setforge:user-section``
comment-marker pair from tracked + live files (shared bodies stay in place; host-
local bodies are captured into the markerless ``local.yaml`` overlay first), then
the parser/reconcile/wizard modules are deleted in the same change.

This module embeds a **frozen, self-contained marker reader** (the grammar copied
verbatim from :mod:`setforge.sections` at author time) so the migration keeps
working AFTER ``sections.py`` is deleted — a fresh host that still carries legacy
markers must be able to migrate. The reader is STRICT and fail-closed: a marker
missing its ``host-local|shared`` keyword, or a pre-rename ``my-setup:`` namespace
marker, is REFUSED with :class:`~setforge.errors.MarkerError` rather than silently
defaulted to SHARED — defaulting a host-local body to shared would leak it into the
shared repo (the lenient-parse trap in :mod:`setforge.sections` this must not inherit).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from setforge.errors import MarkerError

#: The closed set of marker semantics keywords (frozen copy of the sections.py
#: grammar). Kept as the regex alternation the marker line must carry.
_SEMANTICS_KEYWORDS: Final = "host-local|shared"

# Grammar frozen from setforge.sections at author time — see that module's
# docstring for the canonical marker syntax. Kept self-contained so the migration
# survives the deletion of sections.py.
_MARKER_RE: Final = re.compile(
    r"^\s*<!--\s*setforge:user-section\s+(start|end)"
    rf"(?:\s+({_SEMANTICS_KEYWORDS}))?"
    r"(?:\s+(?!hash=)(\S+))?"
    r"(?:\s+hash=(\S+))?"
    r"\s*-->\s*$"
)
# Broad detector: a line that declares one of our markers regardless of payload
# shape — distinguishes "not our marker" from "our marker, malformed".
_MARKER_PREFIX_RE: Final = re.compile(
    r"^\s*<!--\s*setforge:user-section\s+(start|end)\s+(.*?)\s*-->\s*$"
)
# Pre-rename ``my-setup:`` namespace markers — invisible to the post-rename parser,
# so the migration must REFUSE them rather than drop their bodies.
_LEGACY_NAMESPACE_RE: Final = re.compile(
    r"^\s*<!--\s*my-setup:user-section\s+(start|end)\b"
)
_HASH_VALUE_RE: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ParsedSection:
    """One marker-delimited section parsed by the migration's frozen reader.

    ``name`` is the canonical key (the start-marker name, or the 0-based string
    index for an unnamed section, matching :func:`setforge.sections.extract_sections`).
    ``semantics`` is the ``host-local``/``shared`` keyword value. ``body`` is the
    verbatim text between the markers (newlines preserved; ``""`` for an empty
    section). ``embedded_hash`` is the end marker's ``hash=`` baseline, or ``None``.
    """

    name: str
    semantics: str
    body: str
    embedded_hash: str | None


@dataclass(slots=True)
class _Open:
    """Accumulator for the currently-open section during the walk."""

    name: str | None
    key: str
    semantics: str
    body: list[str]


def parse_markers(text: str) -> list[ParsedSection]:
    """STRICT, self-contained parse of ``text`` into its marker sections.

    Byte-equivalent to :func:`setforge.sections.extract_sections` /
    ``section_semantics`` / ``extract_marker_hashes`` for VALID input. Fail-closed:
    raises :class:`~setforge.errors.MarkerError` on a marker missing its
    ``host-local|shared`` keyword, a malformed marker, a ``my-setup:`` legacy
    namespace marker, a nested/unbalanced pair, or a duplicate section key — so a
    host-local body can never be silently reclassified as shared.
    """
    out: list[ParsedSection] = []
    open_section: _Open | None = None
    unnamed_index = 0
    seen_keys: set[str] = set()

    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        if _LEGACY_NAMESPACE_RE.match(line):
            raise MarkerError(
                f"line {lineno}: legacy 'my-setup:user-section' marker — rename the "
                f"namespace to 'setforge:' before migrating (would otherwise lose "
                f"its host-local body)"
            )
        match = _MARKER_RE.match(line)
        if match is None:
            if _MARKER_PREFIX_RE.match(line):
                raise MarkerError(f"line {lineno}: malformed user-section marker")
            if open_section is not None:
                open_section.body.append(line)
            continue

        kind, semantics_kw, name, hash_seg = match.groups()
        if kind == "start":
            open_section = _handle_start(
                lineno, semantics_kw, name, open_section, unnamed_index
            )
            if name is None:
                unnamed_index += 1
        else:
            section = _handle_end(
                lineno, semantics_kw, name, hash_seg, open_section, seen_keys
            )
            out.append(section)
            open_section = None

    if open_section is not None:
        raise MarkerError(f"unclosed user-section {open_section.key!r}")
    return out


def _handle_start(
    lineno: int,
    semantics_kw: str | None,
    name: str | None,
    open_section: _Open | None,
    unnamed_index: int,
) -> _Open:
    """Validate a start marker and open a section (fail-closed on a bad keyword)."""
    if open_section is not None:
        raise MarkerError(f"line {lineno}: nested user-section is not supported")
    if semantics_kw is None:
        raise MarkerError(
            f"line {lineno}: user-section start marker missing the required "
            f"'host-local' or 'shared' keyword (refusing — would leak a host-local "
            f"body if defaulted to shared)"
        )
    key = name if name is not None else str(unnamed_index)
    return _Open(name=name, key=key, semantics=semantics_kw, body=[])


def _handle_end(
    lineno: int,
    semantics_kw: str | None,
    name: str | None,
    hash_seg: str | None,
    open_section: _Open | None,
    seen_keys: set[str],
) -> ParsedSection:
    """Validate an end marker against its open start; return the parsed section."""
    if open_section is None:
        raise MarkerError(f"line {lineno}: user-section end marker without a start")
    if semantics_kw is None:
        raise MarkerError(
            f"line {lineno}: user-section end marker missing the required keyword"
        )
    if semantics_kw != open_section.semantics:
        raise MarkerError(
            f"line {lineno}: end marker semantics {semantics_kw!r} does not match "
            f"start {open_section.semantics!r}"
        )
    if name != open_section.name:
        raise MarkerError(
            f"line {lineno}: end marker name {name!r} does not match start "
            f"{open_section.name!r}"
        )
    if open_section.key in seen_keys:
        raise MarkerError(
            f"line {lineno}: duplicate user-section key {open_section.key!r}"
        )
    seen_keys.add(open_section.key)
    embedded_hash = (
        hash_seg if hash_seg is not None and _HASH_VALUE_RE.match(hash_seg) else None
    )
    return ParsedSection(
        name=open_section.key,
        semantics=open_section.semantics,
        body="".join(open_section.body),
        embedded_hash=embedded_hash,
    )
