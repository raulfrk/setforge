"""User-section marker parsing and merging.

Marker syntax (HTML comments only)::

    <!-- my-setup:user-section start [optional-name] -->
    ... preserved content ...
    <!-- my-setup:user-section end [optional-name] [hash=<sha256-hex>] -->

Tracked files contain marker pairs (with optional placeholder content between
them); on deploy, content from the live file at the corresponding markers is
spliced in. ``merge_sections`` is the splice; ``extract_sections`` is the
inverse used by ``capture`` and by ``compare`` to render a comparable view.

End markers may carry an optional ``hash=<64-char-lowercase-hex>`` segment
that records the sha256 of the section body. Legacy hashless end markers
remain valid.

Nested sections are not supported. End-marker names must match start-marker
names.
"""

import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass

from my_setup.errors import MarkerError

LOGGER: logging.Logger = logging.getLogger(__name__)

_MARKER_RE = re.compile(
    r"^\s*<!--\s*my-setup:user-section\s+(start|end)"
    r"(?:\s+(?!hash=)(\S+))?"
    r"(?:\s+hash=([0-9a-f]{64}))?"
    r"\s*-->\s*$"
)


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


@dataclass(slots=True, frozen=True)
class _EndMarker:
    """A validated user-section end-marker line.

    ``key`` is the section's canonical name (the start-marker name, or the
    0-based string index assigned to unnamed sections in order of appearance).
    ``name`` mirrors the start-marker's name (``None`` for unnamed sections).
    ``embedded_hash`` is the ``hash=<...>`` segment value, or ``None`` if the
    end marker omits it.
    """

    line: str
    name: str | None
    key: str
    embedded_hash: str | None


_MarkerEvent = _BodyLine | _OutsideLine | _StartMarker | _EndMarker


def _walk_markers(text: str) -> Iterator[_MarkerEvent]:
    """Yield one event per line in ``text``, validating marker pairing.

    Centralizes the state machine shared by :func:`extract_sections`,
    :func:`merge_sections`, :func:`extract_marker_hashes`, and
    :func:`set_marker_hashes`: tracks open/closed section state, assigns
    unnamed-section indices, and raises :class:`MarkerError` on nested
    starts, ends-without-starts, name mismatches, and unclosed sections.

    Consumers receive validated, fully-keyed events and only have to do
    their own accumulator / output logic.
    """
    in_section = False
    section_name: str | None = None
    unnamed_index = 0

    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        match = _MARKER_RE.match(line)
        if match is None:
            yield _BodyLine(line) if in_section else _OutsideLine(line)
            continue
        kind, name, embedded_hash = match.group(1), match.group(2), match.group(3)
        if kind == "start":
            if in_section:
                raise MarkerError(
                    f"line {lineno}: nested user-section start "
                    f"(previous section still open)"
                )
            in_section = True
            section_name = name
            yield _StartMarker(line, name)
        else:
            if not in_section:
                raise MarkerError(
                    f"line {lineno}: user-section end without matching start"
                )
            if name != section_name:
                raise MarkerError(
                    f"line {lineno}: user-section end name {name!r} does not "
                    f"match start name {section_name!r}"
                )
            key = section_name if section_name is not None else str(unnamed_index)
            yield _EndMarker(line, section_name, key, embedded_hash)
            if section_name is None:
                unnamed_index += 1
            in_section = False
            section_name = None

    if in_section:
        identifier = section_name if section_name is not None else str(unnamed_index)
        raise MarkerError(f"unclosed user-section (started as {identifier!r})")


def extract_sections(text: str) -> dict[str, str]:
    """Return the content between every marker pair in ``text``.

    Named sections are keyed by their name; unnamed sections are keyed by
    sequential string indices ("0", "1", ...). Section content includes any
    trailing newline up to (but not including) the end-marker line.

    Raises :class:`MarkerError` for nested sections, end-without-start,
    name-mismatched pairs, or unclosed start markers.
    """
    sections: dict[str, str] = {}
    section_lines: list[str] = []
    for event in _walk_markers(text):
        if isinstance(event, _BodyLine):
            section_lines.append(event.line)
        elif isinstance(event, _StartMarker):
            section_lines = []
        elif isinstance(event, _EndMarker):
            sections[event.key] = "".join(section_lines)
            section_lines = []
    return sections


def merge_sections(tracked_text: str, live_sections: dict[str, str]) -> str:
    """Splice ``live_sections`` content into the tracked-file marker regions.

    Sections present in tracked but absent from ``live_sections`` keep
    whatever placeholder content was in the tracked file (this branch is
    used when a fresh dst is being created from a tracked source that ships
    with placeholder text).

    Sections present in ``live_sections`` but absent from tracked are
    dropped with a warning logged via :data:`LOGGER`.
    """
    out_lines: list[str] = []
    placeholder_lines: list[str] = []
    consumed: set[str] = set()

    for event in _walk_markers(tracked_text):
        if isinstance(event, _OutsideLine):
            out_lines.append(event.line)
        elif isinstance(event, _BodyLine):
            placeholder_lines.append(event.line)
        elif isinstance(event, _StartMarker):
            out_lines.append(event.line)
            placeholder_lines = []
        else:  # _EndMarker
            if event.key in live_sections:
                content = live_sections[event.key]
                if content and not content.endswith("\n"):
                    content += "\n"
                out_lines.append(content)
                consumed.add(event.key)
            else:
                out_lines.extend(placeholder_lines)
            out_lines.append(event.line)
            placeholder_lines = []

    for key in sorted(set(live_sections) - consumed):
        LOGGER.warning("live has user-section %r not present in tracked; dropping", key)

    return "".join(out_lines)


def hash_sections(text: str) -> dict[str, str]:
    """Return ``{section-name: sha256-hex-digest}`` for every marker pair.

    Digest is computed over the section body as
    :func:`extract_sections` returns it: 64 lowercase hex chars.
    Coverage-equivalent to :func:`extract_sections`. Raises
    :class:`MarkerError` on the same malformed-marker inputs.
    """
    return {
        name: hashlib.sha256(body.encode("utf-8")).hexdigest()
        for name, body in extract_sections(text).items()
    }


def extract_marker_hashes(text: str) -> dict[str, str | None]:
    """Return ``{section-name: embedded-hash-or-None}`` from end markers.

    ``None`` for sections whose end marker omits ``hash=<...>`` (legacy
    pre-xyw form). Coverage-equivalent to :func:`extract_sections`; raises
    :class:`MarkerError` on the same malformed-marker inputs.
    """
    return {
        event.key: event.embedded_hash
        for event in _walk_markers(text)
        if isinstance(event, _EndMarker)
    }


def set_marker_hashes(text: str, hashes: dict[str, str]) -> str:
    """Rewrite end markers in ``text`` to embed the given hashes.

    For each section whose name is a key in ``hashes``, the end marker is
    rewritten to embed (or replace) its ``hash=<...>`` segment. Sections
    present in ``text`` but absent from ``hashes`` have any existing
    ``hash=<...>`` segment STRIPPED — explicit absence means "no hash for
    this section." Section bodies, start markers, and all non-marker
    content are preserved byte-for-byte.

    Raises :class:`MarkerError` on malformed markers and :class:`ValueError`
    when ``hashes`` contains a key that does not correspond to a section
    present in ``text``.
    """
    # Validate keys up front so a typo fails loudly before any rewrite.
    present = set(extract_sections(text).keys())
    unknown = set(hashes) - present
    if unknown:
        unknown_str = ", ".join(sorted(repr(k) for k in unknown))
        raise ValueError(
            f"set_marker_hashes: hashes contains key(s) not present in text: "
            f"{unknown_str}"
        )

    out_lines: list[str] = []
    for event in _walk_markers(text):
        if isinstance(event, _EndMarker):
            out_lines.append(
                _format_end_marker(event.name, hashes.get(event.key), event.line)
            )
        else:
            out_lines.append(event.line)
    return "".join(out_lines)


def _format_end_marker(
    name: str | None, embedded_hash: str | None, original_line: str
) -> str:
    """Build the canonical end-marker line for ``name`` and ``embedded_hash``.

    Preserves the original line's trailing newline (or its absence) so
    ``set_marker_hashes`` is byte-preserving on file endings.
    """
    parts = ["<!-- my-setup:user-section end"]
    if name is not None:
        parts.append(name)
    if embedded_hash is not None:
        parts.append(f"hash={embedded_hash}")
    parts.append("-->")
    body = " ".join(parts)
    newline = "\n" if original_line.endswith("\n") else ""
    return f"{body}{newline}"


def strip_section_content(text: str) -> str:
    """Return ``text`` with content between every user-section marker pair
    removed. Markers themselves are kept so the file remains a valid
    template for re-merging on a future deploy."""
    out_lines: list[str] = []
    in_section = False
    for line in text.splitlines(keepends=True):
        match = _MARKER_RE.match(line)
        if match is None:
            if not in_section:
                out_lines.append(line)
            continue
        out_lines.append(line)
        in_section = match.group(1) == "start"
    return "".join(out_lines)
