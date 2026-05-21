"""User-section marker parsing and merging.

Marker syntax (HTML comments only)::

    <!-- setforge:user-section start <host-local|shared> NAME -->
    ... preserved content ...
    <!-- setforge:user-section end <host-local|shared> NAME hash=<sha256-hex> -->

The ``host-local|shared`` keyword is REQUIRED on both start and end
markers as of setforge-9by. ``host-local`` sections are preserved
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
:class:`MarkerError` for any marker missing the semantics keyword, any
end marker missing the ``hash=<...>`` segment, OR any ``hash=`` segment
whose value is not exactly 64 lowercase hex chars. The migration-only
escape hatch ``allow_legacy=True`` tolerates all three: missing semantics
parses as :attr:`SectionSemantics.SHARED`; missing or malformed hash
yields ``embedded_hash=None``.
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
from typing import NewType, assert_never

from setforge.errors import MarkerError

LOGGER: logging.Logger = logging.getLogger(__name__)

LiveSections = NewType("LiveSections", dict[str, str])
"""Section bodies parsed from a live file with ``allow_legacy=True``.

Construct via :func:`extract_live_sections`; the install path's pre-9by
migration tolerance lives in that factory so consumer call sites (deploy,
cli) cannot accidentally pass a strict-extract result that would refuse
legacy markers a live file may still carry.
"""


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

# Pre-rename (setforge-2ba.1) namespace detector. Live files deployed
# before the my-setup → setforge rename carry markers like
# ``<!-- my-setup:user-section start ... -->``; the post-rename parser
# (_MARKER_PREFIX_RE above) doesn't recognize them, which would silently
# drop section bodies on the first post-rename install. Used by
# :func:`detect_legacy_namespace_markers` to give the user a clear
# "run sed to migrate" error instead.
_LEGACY_NAMESPACE_RE = re.compile(r"^\s*<!--\s*my-setup:user-section\s+(start|end)\b")

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
    and the next 0-based index to assign to an unnamed section. Mutated
    in place by :func:`_handle_start_marker` and :func:`_handle_end_marker`.
    """

    in_section: bool = False
    section_name: str | None = None
    section_semantics: SectionSemantics | None = None
    unnamed_index: int = 0


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
    state: _WalkState,
) -> _EndMarker:
    """Validate an end marker, mutate ``state``, return the event.

    Raises :class:`MarkerError` on end-without-start, name mismatch with
    the open section, semantics mismatch with the open section, or
    missing ``hash=<...>`` segment when ``allow_legacy`` is false. On
    success, closes the open section and (for unnamed sections)
    increments ``state.unnamed_index``.
    """
    if not state.in_section:
        raise MarkerError(f"line {lineno}: user-section end without matching start")
    if name != state.section_name:
        raise MarkerError(
            f"line {lineno}: user-section end name {name!r} does not "
            f"match start name {state.section_name!r}"
        )
    if semantics is not state.section_semantics:
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
    (``embedded_hash=None``), tolerating pre-9by files that may carry
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


def _walk_markers(text: str, *, allow_legacy: bool = False) -> Iterator[_MarkerEvent]:
    """Yield one event per line in ``text``, validating marker pairing.

    Centralizes the state machine shared by :func:`extract_sections`,
    :func:`merge_sections`, :func:`extract_marker_hashes`,
    :func:`set_marker_hashes`, and :func:`section_semantics`: tracks
    open/closed section state, assigns unnamed-section indices, and
    raises :class:`MarkerError` on nested starts, ends-without-starts,
    name/semantics mismatches, missing-keyword markers, missing
    end-marker hashes, and unclosed sections. Consumers receive
    validated, fully-keyed events and only do their accumulator logic.

    When ``allow_legacy`` is true (migration-only escape hatch used by
    the install path on live-side reads), markers missing the
    ``host-local`` / ``shared`` keyword parse as
    :attr:`SectionSemantics.SHARED`, and end markers missing the
    ``hash=<...>`` segment yield ``embedded_hash=None`` instead of
    raising. All other validation is unaffected.
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
                line, lineno, name, semantics, embedded_hash, allow_legacy, state
            )

    if state.in_section:
        name = state.section_name
        ident = name if name is not None else str(state.unnamed_index)
        raise MarkerError(f"unclosed user-section (started as {ident!r})")


def detect_legacy_namespace_markers(text: str) -> bool:
    """Return ``True`` if ``text`` contains any pre-rename
    ``my-setup:user-section`` marker.

    Detects markers carrying the OLD namespace from before the
    my-setup → setforge rename (setforge-2ba.1). Such markers are
    silently ignored by the post-rename parser; this detector lets
    the CLI surface a clear "run sed migration" error before any
    install/sync/compare run loses host-local section bodies.

    Migration recipe (one-shot per host, per file):

        sed -i 's/my-setup:user-section/setforge:user-section/g' \\
            ~/.claude/CLAUDE.md  # or any other deployed file

    Regex-only scan; no parser invocation. Returns ``True`` on the
    first occurrence found.
    """
    return any(_LEGACY_NAMESPACE_RE.match(line) for line in text.splitlines())


def detect_legacy_markers(text: str) -> bool:
    """Return ``True`` if ``text`` contains any pre-9by-form marker.

    Regex-only scan (does NOT call :func:`_walk_markers`). Used by the
    CLI layer to surface a "run install first" error before the strict
    parser's :class:`MarkerError` propagates as a raw ``line N: missing
    keyword`` / ``missing hash`` message.

    A marker is "legacy" if its semantics keyword is missing OR (for
    end markers) its ``hash=<...>`` segment is missing OR its
    ``hash=<...>`` segment is present but not a 64-char hex digest.
    Returns ``True`` on the first such marker found.
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
        if (
            kind == "end"
            and embedded_hash is not None
            and not _HASH_VALUE_RE.fullmatch(embedded_hash)
        ):
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


def extract_live_sections(text: str) -> LiveSections:
    """Parse ``text`` into a :class:`LiveSections` using ``allow_legacy=True``.

    The single legitimate constructor for :class:`LiveSections`. Install is
    the verb that re-tags and stamps pre-9by markers in place, so the
    install path's live-side parsing opts into the migration-only legacy
    tolerance here; compare / sync remain strict by routing through
    :func:`extract_sections` directly.
    """
    return LiveSections(extract_sections(text, allow_legacy=True))


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

    # The uniform line=line capture is the selective style here: every
    # arm of this cascade legitimately consumes ``line``. Compare
    # ``extract_sections`` above where some arms (e.g. ``_StartMarker()``)
    # genuinely don't need the capture and elide it.
    for event in _walk_markers(tracked_text):
        match event:
            case _OutsideLine(line=line):
                out_lines.append(line)
            case _BodyLine(line=line):
                placeholder_lines.append(line)
            case _StartMarker(line=line):
                out_lines.append(line)
                placeholder_lines = []
            case _EndMarker(key=key, line=line):
                if key in live_sections:
                    content = live_sections[key]
                    if content and not content.endswith("\n"):
                        content += "\n"
                    out_lines.append(content)
                    consumed.add(key)
                else:
                    out_lines.extend(placeholder_lines)
                out_lines.append(line)
                placeholder_lines = []
            case _ as never:
                assert_never(never)

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
        match event:
            case _EndMarker(name=name, semantics=semantics, key=key, line=line):
                out_lines.append(
                    _format_end_marker(name, semantics, hashes.get(key), line)
                )
            case (
                _BodyLine(line=line) | _OutsideLine(line=line) | _StartMarker(line=line)
            ):
                out_lines.append(line)
            case _ as never:
                assert_never(never)
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
    parts = ["<!-- setforge:user-section end", semantics.value]
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
        event.key: event.semantics
        for event in _walk_markers(text, allow_legacy=allow_legacy)
        if isinstance(event, _EndMarker)
    }


def strip_host_local_sections(
    text: str, *, names: frozenset[str], allow_legacy: bool = True
) -> str:
    """Return ``text`` with named host-local marker pairs (markers + body) removed.

    Used by the capture path (:func:`setforge.capture.capture_tracked_file`)
    to prevent host-local sections injected by ``setforge install`` (from
    local.yaml ``host_local_sections``) from leaking back into tracked
    sources on the next ``setforge sync``. ``names`` is the set of
    host-local section names declared in local.yaml — only those pairs
    are removed; any host-local marker pair the user authored directly
    in tracked (carried through to live) passes through unchanged.
    Shared marker pairs always pass through. ``allow_legacy`` mirrors
    the strip/extract default — capture reads live-side text that may
    contain pre-9by markers.

    No-op when ``names`` is empty.
    """
    if not names:
        return text
    out_lines: list[str] = []
    drop = False
    for event in _walk_markers(text, allow_legacy=allow_legacy):
        match event:
            case _StartMarker(semantics=SectionSemantics.HOST_LOCAL, name=name) if (
                name in names
            ):
                drop = True
                continue
            case _EndMarker(semantics=SectionSemantics.HOST_LOCAL, key=key) if (
                key in names
            ):
                drop = False
                continue
            case _BodyLine(line=line):
                if not drop:
                    out_lines.append(line)
            case (
                _OutsideLine(line=line)
                | _StartMarker(line=line)
                | _EndMarker(line=line)
            ):
                out_lines.append(line)
            case _ as never:
                assert_never(never)
    return "".join(out_lines)


def strip_section_content(text: str, *, allow_legacy: bool = True) -> str:
    """Return ``text`` with content between every user-section marker pair
    removed. Markers themselves are kept so the file remains a valid
    template for re-merging on a future deploy.

    ``allow_legacy`` defaults to ``True`` because the historical callers
    (compare's drift gate, capture's strip path) both consume live-side
    text that may contain pre-9by markers. Pass ``allow_legacy=False``
    explicitly where canonical-form input is guaranteed and you want
    strict parsing to surface malformed input. Symmetric comparisons
    across (tracked, live) — e.g. ``compare.diff_file``'s strip-template
    gate at ``strip_section_content(src) == strip_section_content(dst)``
    — pass ``allow_legacy=True`` on the tracked side too because the
    live side may carry pre-9by markers; mixing regimes is safe only
    when each side independently satisfies its own regime (cf. the
    sibling ``hash_sections`` call at the same gate, which mixes
    strict-tracked + lenient-live successfully).
    """
    out_lines: list[str] = []
    for event in _walk_markers(text, allow_legacy=allow_legacy):
        match event:
            case _BodyLine():
                pass
            case (
                _OutsideLine(line=line)
                | _StartMarker(line=line)
                | _EndMarker(line=line)
            ):
                out_lines.append(line)
            case _ as never:
                assert_never(never)
    return "".join(out_lines)
