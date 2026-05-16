"""User-section marker parsing and merging.

Marker syntax (HTML comments only)::

    <!-- my-setup:user-section start <host-local|shared> NAME -->
    ... preserved content ...
    <!-- my-setup:user-section end <host-local|shared> NAME hash=<sha256-hex> -->

The ``host-local|shared`` keyword is REQUIRED on both start and end
markers as of dotfiles-9by. ``host-local`` sections are preserved
unconditionally from live on install; ``shared`` sections participate in
a three-way merge that can surface tracked-side updates via the
``--reconcile-user-sections`` wizard. End markers carry a mandatory
``hash=<64-char-lowercase-hex>`` segment recording the sha256 of the
section body — install rewrites these on every write to keep them
aligned with the body actually written.

Tracked files contain marker pairs (with optional placeholder content between
them); on deploy, content from the live file at the corresponding markers is
spliced in. ``merge_sections`` is the splice; ``extract_sections`` is the
inverse used by ``capture`` and by ``compare`` to render a comparable view.

The strict parser (``allow_legacy=False``, the default) raises
:class:`MarkerError` for any marker missing the semantics keyword OR any
end marker missing the ``hash=<...>`` segment. The migration-only escape
hatch ``allow_legacy=True`` tolerates both: missing semantics parses as
:attr:`SectionSemantics.SHARED`; missing hash yields ``embedded_hash=None``.
Only the install path's live-side parsing opts in via ``allow_legacy=True``
so pre-9by user files can be migrated in place on first install; compare /
sync remain strict and surface a user-actionable error before the raw
:class:`MarkerError` propagates. Start and end keywords must match. Nested
sections are not supported. End-marker names must match start-marker names.
"""

import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from my_setup.errors import MarkerError

LOGGER: logging.Logger = logging.getLogger(__name__)


class SectionSemantics(StrEnum):
    """Closed set of user-section marker semantics keywords."""

    HOST_LOCAL = "host-local"
    SHARED = "shared"


_SEMANTICS_KEYWORDS = "host-local|shared"

_MARKER_RE = re.compile(
    r"^\s*<!--\s*my-setup:user-section\s+(start|end)"
    rf"(?:\s+({_SEMANTICS_KEYWORDS}))?"
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


def _walk_markers(text: str, *, allow_legacy: bool = False) -> Iterator[_MarkerEvent]:
    """Yield one event per line in ``text``, validating marker pairing.

    Centralizes the state machine shared by :func:`extract_sections`,
    :func:`merge_sections`, :func:`extract_marker_hashes`,
    :func:`set_marker_hashes`, and :func:`section_semantics`: tracks
    open/closed section state, assigns unnamed-section indices, and
    raises :class:`MarkerError` on nested starts, ends-without-starts,
    name mismatches, semantics-keyword mismatches, missing-keyword
    markers, missing end-marker hashes, and unclosed sections.

    When ``allow_legacy`` is ``True`` (migration-only escape hatch used
    by the install path on live-side reads), markers missing the
    ``host-local`` / ``shared`` keyword parse as
    :attr:`SectionSemantics.SHARED`, and end markers missing the
    ``hash=<...>`` segment yield ``embedded_hash=None`` instead of
    raising. All other validation (nesting, name match, semantics
    match, unclosed) is unaffected.

    Consumers receive validated, fully-keyed events and only have to do
    their own accumulator / output logic.
    """
    in_section = False
    section_name: str | None = None
    section_semantics: SectionSemantics | None = None
    unnamed_index = 0

    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        match = _MARKER_RE.match(line)
        if match is None:
            yield _BodyLine(line) if in_section else _OutsideLine(line)
            continue
        kind = match.group(1)
        semantics_raw = match.group(2)
        name = match.group(3)
        embedded_hash = match.group(4)
        if semantics_raw is None:
            if not allow_legacy:
                raise MarkerError(
                    f"line {lineno}: user-section {kind} marker missing required "
                    f"'host-local' or 'shared' keyword"
                )
            semantics = SectionSemantics.SHARED
        else:
            semantics = SectionSemantics(semantics_raw)
        if kind == "start":
            if in_section:
                raise MarkerError(
                    f"line {lineno}: nested user-section start "
                    f"(previous section still open)"
                )
            in_section = True
            section_name = name
            section_semantics = semantics
            yield _StartMarker(line, name, semantics)
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
            if semantics is not section_semantics:
                raise MarkerError(
                    f"line {lineno}: user-section end semantics {semantics.value!r} "
                    f"does not match start semantics "
                    f"{section_semantics.value if section_semantics else None!r}"
                )
            if embedded_hash is None and not allow_legacy:
                raise MarkerError(
                    f"line {lineno}: user-section end marker missing required "
                    f"'hash=<sha256-hex>' segment"
                )
            key = section_name if section_name is not None else str(unnamed_index)
            yield _EndMarker(line, section_name, key, semantics, embedded_hash)
            if section_name is None:
                unnamed_index += 1
            in_section = False
            section_name = None
            section_semantics = None

    if in_section:
        identifier = section_name if section_name is not None else str(unnamed_index)
        raise MarkerError(f"unclosed user-section (started as {identifier!r})")


def detect_legacy_markers(text: str) -> bool:
    """Return ``True`` if ``text`` contains any pre-9by-form marker.

    Regex-only scan (does NOT call :func:`_walk_markers`). Used by the
    CLI layer to surface a "run install first" error before the strict
    parser's :class:`MarkerError` propagates as a raw ``line N: missing
    keyword`` / ``missing hash`` message.

    A marker is "legacy" if EITHER its semantics keyword is missing OR
    (for end markers) its ``hash=<...>`` segment is missing. Returns
    ``True`` on the first such marker found.
    """
    for line in text.splitlines():
        match = _MARKER_RE.match(line)
        if match is None:
            continue
        kind, semantics_raw, _name, embedded_hash = match.groups()
        if semantics_raw is None:
            return True
        if kind == "end" and embedded_hash is None:
            return True
    return False


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
    install path's migration-only mode for pre-9by live files).
    """
    sections: dict[str, str] = {}
    section_lines: list[str] = []
    for event in _walk_markers(text, allow_legacy=allow_legacy):
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


def hash_sections(text: str, *, allow_legacy: bool = False) -> dict[str, str]:
    """Return ``{section-name: sha256-hex-digest}`` for every marker pair.

    Digest is computed over the section body as
    :func:`extract_sections` returns it: 64 lowercase hex chars.
    Coverage-equivalent to :func:`extract_sections`. Raises
    :class:`MarkerError` on the same malformed-marker inputs. Pass
    ``allow_legacy=True`` to tolerate pre-9by markers (see
    :func:`_walk_markers`).
    """
    return {
        name: hashlib.sha256(body.encode("utf-8")).hexdigest()
        for name, body in extract_sections(text, allow_legacy=allow_legacy).items()
    }


def extract_marker_hashes(
    text: str, *, allow_legacy: bool = False
) -> dict[str, str | None]:
    """Return ``{section-name: embedded-hash-or-None}`` from end markers.

    ``None`` for sections whose end marker omits ``hash=<...>`` — only
    reachable under ``allow_legacy=True``; the strict default raises on
    missing-hash markers. Coverage-equivalent to
    :func:`extract_sections`; raises :class:`MarkerError` on the same
    malformed-marker inputs.
    """
    return {
        event.key: event.embedded_hash
        for event in _walk_markers(text, allow_legacy=allow_legacy)
        if isinstance(event, _EndMarker)
    }


def set_marker_hashes(
    text: str, hashes: dict[str, str], *, allow_legacy: bool = False
) -> str:
    """Rewrite end markers in ``text`` to embed the given hashes.

    For each section whose name is a key in ``hashes``, the end marker is
    rewritten to embed (or replace) its ``hash=<...>`` segment. Sections
    present in ``text`` but absent from ``hashes`` have any existing
    ``hash=<...>`` segment STRIPPED — explicit absence means "no hash for
    this section." Section bodies, start markers, and all non-marker
    content are preserved byte-for-byte. Pass ``allow_legacy=True`` to
    tolerate pre-9by input markers (see :func:`_walk_markers`); the
    output markers are always fully formed.

    Raises :class:`MarkerError` on malformed markers and :class:`ValueError`
    when ``hashes`` contains a key that does not correspond to a section
    present in ``text``.
    """
    # Validate keys up front so a typo fails loudly before any rewrite.
    present = set(extract_sections(text, allow_legacy=allow_legacy).keys())
    unknown = set(hashes) - present
    if unknown:
        unknown_str = ", ".join(sorted(repr(k) for k in unknown))
        raise ValueError(
            f"set_marker_hashes: hashes contains key(s) not present in text: "
            f"{unknown_str}"
        )

    out_lines: list[str] = []
    for event in _walk_markers(text, allow_legacy=allow_legacy):
        if isinstance(event, _EndMarker):
            out_lines.append(
                _format_end_marker(
                    event.name, event.semantics, hashes.get(event.key), event.line
                )
            )
        else:
            out_lines.append(event.line)
    return "".join(out_lines)


def _format_end_marker(
    name: str | None,
    semantics: SectionSemantics,
    embedded_hash: str | None,
    original_line: str,
) -> str:
    """Build the canonical end-marker line for ``name`` and ``embedded_hash``.

    Preserves the original line's trailing newline (or its absence) so
    ``set_marker_hashes`` is byte-preserving on file endings.
    """
    parts = ["<!-- my-setup:user-section end", semantics.value]
    if name is not None:
        parts.append(name)
    if embedded_hash is not None:
        parts.append(f"hash={embedded_hash}")
    parts.append("-->")
    body = " ".join(parts)
    newline = "\n" if original_line.endswith("\n") else ""
    return f"{body}{newline}"


def section_semantics(
    text: str, *, allow_legacy: bool = False
) -> dict[str, SectionSemantics]:
    """Return ``{section-name: SectionSemantics}`` for every marker pair.

    Coverage-equivalent to :func:`extract_sections`; raises
    :class:`MarkerError` on the same malformed-marker inputs. Values
    are :class:`SectionSemantics` enum members; since ``SectionSemantics``
    is a :class:`StrEnum`, callers may compare against ``"host-local"`` /
    ``"shared"`` directly (``SectionSemantics.SHARED == "shared"``). Pass
    ``allow_legacy=True`` to tolerate pre-9by markers (untagged markers
    parse as :attr:`SectionSemantics.SHARED`).
    """
    return {
        event.key: event.semantics  # type: ignore[misc]
        for event in _walk_markers(text, allow_legacy=allow_legacy)
        if isinstance(event, _EndMarker)
    }


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
