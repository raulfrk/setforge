"""Frozen author-time copy of the user-section marker reader — do not edit.

A VERBATIM, self-contained freeze of the legacy marker-parse machinery the
migration chain still needs after the parser modules are deleted. Two migration
consumers read pre-2.1 marker bytes through this module:

* :mod:`setforge.migrations._contract_2_0` (1.2 -> 2.0) enumerates the marked
  sections in a tracked src via :func:`extract_sections` / :func:`section_semantics`
  (``allow_legacy=True``) to translate ``preserve_user_sections`` into spans;
* :mod:`setforge.host_local_marker_migration` (1.1 -> 1.2) walks the live-file
  markers via :func:`_walk_markers` to capture host-local bodies into overlays.

The grammar was copied byte-for-byte from the retired ``setforge._legacy_markers``
at author time so a permanent old-schema migration keeps reading marker bytes
after that module is gone. This module imports ZERO of the retired parser
modules. Treat it as frozen: do not extend or re-flow it.

Marker syntax (HTML comments only)::

    <!-- setforge:user-section start <host-local|shared> NAME -->
    ... preserved content ...
    <!-- setforge:user-section end <host-local|shared> NAME hash=<sha256-hex> -->

The strict parser (``allow_legacy=False``, the default) raises
:class:`MarkerError` for any marker missing the semantics keyword, any
end marker missing the ``hash=<...>`` segment, OR any ``hash=`` segment
whose value is not exactly 64 lowercase hex chars. The migration-only
escape hatch ``allow_legacy=True`` tolerates all three: missing semantics
parses as :attr:`SectionSemantics.SHARED`; missing or malformed hash
yields ``embedded_hash=None``. Start and end keywords must match. Nested
sections are not supported. End-marker names must match start-marker names.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import assert_never

from setforge.errors import MarkerError


class SectionSemantics(StrEnum):
    """Closed set of user-section marker semantics keywords."""

    HOST_LOCAL = "host-local"
    SHARED = "shared"


_SEMANTICS_KEYWORDS = "host-local|shared"

_MARKER_RE = re.compile(
    r"^\s*<!--\s*setforge:user-section\s+(start|end)"
    rf"(?:\s+({_SEMANTICS_KEYWORDS}))?"
    r"(?:\s+(?!hash=)(\S+))?"
    r"(?:\s+hash=(\S+))?"
    r"\s*-->\s*$"
)

# Broad detector: matches any line whose prefix declares it as one of our
# markers, regardless of payload shape. Used by :func:`_parse_marker_line` to
# distinguish "not our marker at all" from "our marker, but malformed" so the
# latter surfaces a precise :class:`MarkerError` instead of being silently
# dropped as outside-content. Captures (kind, rest-before-`-->`).
_MARKER_PREFIX_RE = re.compile(
    r"^\s*<!--\s*setforge:user-section\s+(start|end)\s+(.*?)\s*-->\s*$"
)

_HASH_VALUE_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(slots=True, frozen=True)
class _BodyLine:
    """A line inside a section body (between a start and its end marker)."""

    line: str


@dataclass(slots=True, frozen=True)
class _OutsideLine:
    """A line outside any section (no enclosing marker pair)."""

    line: str


@dataclass(slots=True, frozen=True)
class _StartMarker:
    """A validated user-section start-marker line."""

    line: str
    name: str | None
    semantics: SectionSemantics


@dataclass(slots=True, frozen=True)
class _EndMarker:
    """A validated user-section end-marker line.

    ``key`` is the section's canonical name (the start-marker name, or the
    0-based string index assigned to unnamed sections in order of appearance).
    ``name`` mirrors the start-marker's name (``None`` for unnamed sections).
    ``semantics`` mirrors the start-marker's semantics keyword.
    ``embedded_hash`` is the ``hash=<...>`` segment value, or ``None`` if the
    end marker omits it.
    """

    line: str
    name: str | None
    key: str
    semantics: SectionSemantics
    embedded_hash: str | None


_MarkerEvent = _BodyLine | _OutsideLine | _StartMarker | _EndMarker


@dataclass(slots=True)
class _WalkState:
    """Mutable state machine accumulator for :func:`_walk_markers`.

    Tracks the currently-open section (``None`` when no section is open)
    and the next 0-based index to assign to an unnamed section. ``seen_keys``
    records every section key already closed so a second pair sharing one
    key is rejected (it would otherwise collapse silently in the dict-keyed
    primitives). Mutated in place by :func:`_handle_start_marker` and
    :func:`_handle_end_marker`.
    """

    in_section: bool = False
    section_name: str | None = None
    section_semantics: SectionSemantics | None = None
    unnamed_index: int = 0
    seen_keys: set[str] = field(default_factory=set)


def _handle_start_marker(
    line: str,
    lineno: int,
    name: str | None,
    semantics: SectionSemantics,
    state: _WalkState,
) -> _StartMarker:
    """Validate a start marker, mutate ``state``, return the event.

    Raises :class:`MarkerError` when a section is already open (nested
    start). On success, marks ``state.in_section`` true and records the
    new section's name and semantics.
    """
    if state.in_section:
        raise MarkerError(
            f"line {lineno}: nested user-section start (previous section still open)"
        )
    state.in_section = True
    state.section_name = name
    state.section_semantics = semantics
    return _StartMarker(line, name, semantics)


def _handle_end_marker(
    line: str,
    lineno: int,
    name: str | None,
    semantics: SectionSemantics,
    embedded_hash: str | None,
    allow_legacy: bool,
    reject_duplicate_keys: bool,
    state: _WalkState,
) -> _EndMarker:
    """Validate an end marker, mutate ``state``, return the event.

    Raises :class:`MarkerError` on end-without-start, name mismatch with
    the open section, semantics mismatch with the open section, or missing
    ``hash=<...>`` segment when ``allow_legacy`` is false. When
    ``reject_duplicate_keys`` is true, also raises on a section key already
    closed earlier in the same text — a duplicate name would otherwise
    collapse silently in the dict-keyed primitives, splicing one surviving
    body into both regions and corrupting the first region's end-marker
    hash. The line-iterating strip/migration helpers leave the flag false
    so they can still de-marker a live file that already carries duplicate
    pairs. On success, closes the open section and (for unnamed sections)
    increments ``state.unnamed_index``.
    """
    if not state.in_section:
        raise MarkerError(f"line {lineno}: user-section end without matching start")
    if name != state.section_name:
        raise MarkerError(
            f"line {lineno}: user-section end name {name!r} does not "
            f"match start name {state.section_name!r}"
        )
    if semantics != state.section_semantics:
        raise MarkerError(
            f"line {lineno}: user-section end semantics {semantics.value!r} "
            f"does not match start semantics "
            f"{state.section_semantics.value if state.section_semantics else None!r}"
        )
    if embedded_hash is None and not allow_legacy:
        raise MarkerError(
            f"line {lineno}: user-section end marker missing required "
            f"'hash=<sha256-hex>' segment"
        )
    key = (
        state.section_name
        if state.section_name is not None
        else str(state.unnamed_index)
    )
    if reject_duplicate_keys and key in state.seen_keys:
        raise MarkerError(f"line {lineno}: duplicate user-section name {key!r}")
    state.seen_keys.add(key)
    event = _EndMarker(line, state.section_name, key, semantics, embedded_hash)
    if state.section_name is None:
        state.unnamed_index += 1
    state.in_section = False
    state.section_name = None
    state.section_semantics = None
    return event


def _raise_if_malformed_marker(line: str, lineno: int) -> None:
    """Raise :class:`MarkerError` when ``line`` looks like one of our markers
    but is malformed (currently: unknown semantics keyword).

    Called by :func:`_parse_marker_line` after the strict ``_MARKER_RE`` has
    failed. Without this gate, a marker like
    ``<!-- setforge:user-section start fish-tacos NAME -->`` would be
    silently treated as outside-content; the user would see no error or
    an opaque downstream "end-without-start" instead of the precise
    "unknown semantics keyword" with line context.

    Today only the unknown-semantics case is detected here; other
    malformed shapes (e.g. trailing junk after ``-->``) still parse as
    non-markers because they don't satisfy the broad-prefix matcher.
    """
    broad = _MARKER_PREFIX_RE.match(line)
    if broad is None:
        return
    kind = broad.group(1)
    rest = broad.group(2)
    tokens = rest.split()
    if not tokens:
        return
    first = tokens[0]
    if first in {s.value for s in SectionSemantics}:
        return
    # A ``hash=`` token in the semantics position (token 1) is always
    # malformed — the strict syntax is ``<kind> <semantics> [NAME]
    # [hash=<sha>]`` and ``hash=`` only appears in position 3 on end
    # markers. Surface this distinctly so the user sees "you forgot the
    # semantics keyword" rather than "unknown semantics keyword
    # 'hash=abc'".
    if first.startswith("hash="):
        raise MarkerError(
            f"line {lineno}: user-section {kind} marker is missing the "
            f"semantics keyword before {first!r}; expected "
            f"'host-local' or 'shared' as the first token"
        )
    raise MarkerError(
        f"line {lineno}: user-section {kind} marker has unknown semantics "
        f"keyword {first!r}; expected 'host-local' or 'shared'"
    )


def _parse_marker_line(
    line: str, lineno: int, *, allow_legacy: bool
) -> tuple[str, str, str | None, str | None] | None:
    """Parse one line and return marker components or None for non-markers.

    Returns ``(kind, semantics_value, name, embedded_hash)`` when
    ``line`` matches ``_MARKER_RE``, where:
    - ``kind`` is ``"start"`` or ``"end"``.
    - ``semantics_value`` is the matched semantics keyword
      (``"host-local"`` or ``"shared"``).
    - ``name`` is the section name or ``None`` for unnamed sections.
    - ``embedded_hash`` is the ``hash=<64-hex>`` value or ``None``.

    Returns ``None`` when the line is not a marker line (body content
    or outside-any-section content).

    Raises :class:`MarkerError` on a marker line missing its semantics
    keyword when ``allow_legacy=False``. When ``allow_legacy=True``, a
    missing semantics is treated as ``"shared"``. The missing-hash
    check on ``end`` markers is deferred to the state machine in
    :func:`_walk_markers` so it fires AFTER name/semantics-mismatch
    validation (preserving pre-extraction error ordering).

    Also raises :class:`MarkerError` when the captured ``hash=`` segment
    is present but not exactly 64 lowercase hex chars and
    ``allow_legacy=False``; under ``allow_legacy=True`` a malformed
    ``hash=`` is treated as if the segment were absent
    (``embedded_hash=None``), tolerating pre-hash files that may carry
    garbled hash values.
    """
    match = _MARKER_RE.match(line)
    if match is None:
        _raise_if_malformed_marker(line, lineno)
        return None
    kind = match.group(1)
    semantics_raw = match.group(2)
    name = match.group(3)
    embedded_hash = match.group(4)
    if embedded_hash is not None and not _HASH_VALUE_RE.fullmatch(embedded_hash):
        if not allow_legacy:
            raise MarkerError(
                f"line {lineno}: malformed hash= segment {embedded_hash!r}; "
                f"expected 64 lowercase hex chars"
            )
        embedded_hash = None
    if semantics_raw is None:
        if not allow_legacy:
            raise MarkerError(
                f"line {lineno}: user-section {kind} marker missing "
                f"required 'host-local' or 'shared' keyword"
            )
        semantics_raw = SectionSemantics.SHARED.value
    return kind, semantics_raw, name, embedded_hash


def _walk_markers(
    text: str,
    *,
    allow_legacy: bool = False,
    reject_duplicate_keys: bool = False,
) -> Iterator[_MarkerEvent]:
    """Yield one event per line in ``text``, validating marker pairing.

    Centralizes the state machine shared by :func:`extract_sections` and
    :func:`section_semantics`: tracks open/closed section state, assigns
    unnamed-section indices, and raises :class:`MarkerError` on nested
    starts, ends-without-starts, name/semantics mismatches, missing-keyword
    markers, missing end-marker hashes, and unclosed sections. Consumers
    receive validated, fully-keyed events and only do their accumulator logic.

    When ``allow_legacy`` is true (migration-only escape hatch used by
    the install path on live-side reads), markers missing the
    ``host-local`` / ``shared`` keyword parse as
    :attr:`SectionSemantics.SHARED`, and end markers missing the
    ``hash=<...>`` segment yield ``embedded_hash=None`` instead of
    raising. All other validation is unaffected.

    ``reject_duplicate_keys`` (default false) makes a second section pair
    sharing one key raise :class:`MarkerError` instead of yielding it. The
    dict-keyed primitives (:func:`extract_sections`, :func:`section_semantics`)
    pass it true, because there a duplicate name silently drops the first
    body. The default stays false so a line-iterating consumer that
    deliberately collects every same-named match keeps its event-by-event
    behavior.
    """
    state = _WalkState()
    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        parsed = _parse_marker_line(line, lineno, allow_legacy=allow_legacy)
        if parsed is None:
            yield _BodyLine(line) if state.in_section else _OutsideLine(line)
            continue
        kind, semantics_raw, name, embedded_hash = parsed
        semantics = SectionSemantics(semantics_raw)
        if kind == "start":
            yield _handle_start_marker(line, lineno, name, semantics, state)
        else:
            yield _handle_end_marker(
                line,
                lineno,
                name,
                semantics,
                embedded_hash,
                allow_legacy,
                reject_duplicate_keys,
                state,
            )

    if state.in_section:
        name = state.section_name
        ident = name if name is not None else str(state.unnamed_index)
        raise MarkerError(f"unclosed user-section (started as {ident!r})")


def extract_sections(text: str, *, allow_legacy: bool = False) -> dict[str, str]:
    """Return the content between every marker pair in ``text``.

    Named sections are keyed by their name; unnamed sections are keyed by
    sequential string indices ("0", "1", ...). Section content includes any
    trailing newline up to (but not including) the end-marker line.

    Raises :class:`MarkerError` for nested sections, end-without-start,
    name-mismatched pairs, or unclosed start markers. With the strict
    default (``allow_legacy=False``) also raises on markers missing the
    ``host-local``/``shared`` keyword or end markers missing
    ``hash=<...>``; pass ``allow_legacy=True`` to tolerate both (the
    install path's migration-only mode for pre-hash live files).
    """
    sections: dict[str, str] = {}
    section_lines: list[str] = []
    for event in _walk_markers(
        text, allow_legacy=allow_legacy, reject_duplicate_keys=True
    ):
        match event:
            case _BodyLine(line=line):
                section_lines.append(line)
            case _StartMarker():
                section_lines = []
            case _EndMarker(key=key):
                sections[key] = "".join(section_lines)
                section_lines = []
            case _OutsideLine():
                pass
            case _ as never:
                assert_never(never)
    return sections


def section_semantics(
    text: str, *, allow_legacy: bool = False
) -> dict[str, SectionSemantics]:
    """Return ``{section-name: SectionSemantics}`` for every marker pair.

    Coverage-equivalent to :func:`extract_sections`; raises
    :class:`MarkerError` on the same malformed-marker inputs. Values
    are :class:`SectionSemantics` enum members; since ``SectionSemantics``
    is a :class:`StrEnum`, callers may compare against ``"host-local"`` /
    ``"shared"`` directly (``SectionSemantics.SHARED == "shared"``). Pass
    ``allow_legacy=True`` to tolerate pre-hash markers (untagged markers
    parse as :attr:`SectionSemantics.SHARED`).
    """
    return {
        event.key: event.semantics
        for event in _walk_markers(
            text, allow_legacy=allow_legacy, reject_duplicate_keys=True
        )
        if isinstance(event, _EndMarker)
    }
