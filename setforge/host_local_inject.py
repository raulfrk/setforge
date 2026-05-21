"""Host-local user-section injection for markdown tracked_files (setforge-xsco).

Resolves a :data:`setforge.source.Anchor` against rendered markdown text
and splices a host-local user-section marker pair + body at the resolved
line offset. The injection routes every marker construction through
:func:`setforge.cli.section._format_marker_pair_unstamped` +
:func:`setforge.cli.section._stamp_section_hashes` so the post-install
hash invariant (``extract_marker_hashes(text) == hash_sections(text)``)
holds.

Anchor grammar:

* ``after-heading`` / ``before-heading`` — match by exact heading text
  (byte-equal, no slugify / case-fold). Lines inside fenced code blocks
  (``` ``` ```) are skipped during the scan so a heading-shaped string
  in a code example does not collide with a real heading. Duplicate
  matches raise :class:`AnchorAmbiguousError`.
* ``at-start-of-file`` — splice at line offset 0 (file head).
* ``at-end-of-file`` — splice at the line after the last line of the
  file (file tail; a trailing newline is added if missing).
* ``after-section`` — splice after the end marker of an existing
  user-section in the SAME tracked file. Duplicate section names with
  the same key raise :class:`AnchorAmbiguousError`.

All zero / multiple-match cases raise an :class:`AnchorNotFoundError` or
:class:`AnchorAmbiguousError` (both subclasses of
:class:`setforge.errors.ConfigError`) BEFORE any file write — install
aborts cleanly without modifying any tracked or live file.

Live-side text is normalised at the splice boundary
(``text.replace("\\r\\n", "\\n")``) so a CRLF live file does not
desync with the LF tracked file's section boundaries (anti-smell
item 11).
"""

from __future__ import annotations

import re
from typing import assert_never

from setforge.errors import AnchorAmbiguousError, AnchorNotFoundError
from setforge.sections import (
    SectionSemantics,
    _EndMarker,
    _walk_markers,
)
from setforge.source import (
    Anchor,
    AnchorAfterHeading,
    AnchorAfterSection,
    AnchorAtEndOfFile,
    AnchorAtStartOfFile,
    AnchorBeforeHeading,
    HostLocalSection,
)

# Matches an ATX-style markdown heading: 1-6 leading ``#`` followed by a
# space and the heading text. Setext (underline-style) headings are
# intentionally NOT supported — the anchor grammar is byte-exact text
# match against the trimmed heading content, which has no analogue for
# setext where the heading is the line ABOVE the ``===`` / ``---`` rule.
_HEADING_RE: re.Pattern[str] = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")

# Fenced-code-block opener / closer. Match the standard ``` and ~~~
# fences with optional info string. The scanner toggles a flag whenever
# this matches at column 0 (commonmark requires up-to-3 leading spaces;
# we accept those too via the leading whitespace class).
_FENCE_RE: re.Pattern[str] = re.compile(r"^\s{0,3}(```|~~~)")


def _normalise_eol(text: str) -> str:
    """Return ``text`` with CRLF and CR line endings collapsed to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _find_heading_offsets(text: str, heading: str) -> list[int]:
    """Return every 0-indexed line offset whose heading text equals ``heading``.

    Skips lines inside fenced code blocks. Matches by trimmed heading
    content only — leading ``#`` characters and surrounding whitespace
    are stripped before comparison; the ``#`` depth is otherwise
    irrelevant (``## Foo`` matches anchor ``Foo``).
    """
    matches: list[int] = []
    in_fence = False
    for idx, line in enumerate(text.splitlines()):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        if match.group(1) == heading:
            matches.append(idx)
    return matches


def _resolve_after_heading(text: str, anchor: AnchorAfterHeading) -> int:
    """Return the line offset immediately after the matched heading line.

    Splices BELOW the heading — the marker pair lands on the line that
    used to be heading+1 in the file. Raises :class:`AnchorNotFoundError`
    on zero matches; :class:`AnchorAmbiguousError` on more than one.
    """
    matches = _find_heading_offsets(text, anchor.value)
    if not matches:
        raise AnchorNotFoundError(
            f"no heading matched anchor after-heading {anchor.value!r}"
        )
    if len(matches) > 1:
        lines_1 = ", ".join(str(m + 1) for m in matches)
        raise AnchorAmbiguousError(
            f"anchor after-heading {anchor.value!r} matches multiple "
            f"headings at lines {lines_1}; rename one or pick a more specific value"
        )
    return matches[0] + 1


def _resolve_before_heading(text: str, anchor: AnchorBeforeHeading) -> int:
    """Return the line offset of the matched heading line itself.

    Splices ABOVE the heading — the marker pair lands on the line that
    used to be the heading; the heading itself shifts down.
    """
    matches = _find_heading_offsets(text, anchor.value)
    if not matches:
        raise AnchorNotFoundError(
            f"no heading matched anchor before-heading {anchor.value!r}"
        )
    if len(matches) > 1:
        lines_1 = ", ".join(str(m + 1) for m in matches)
        raise AnchorAmbiguousError(
            f"anchor before-heading {anchor.value!r} matches multiple "
            f"headings at lines {lines_1}; rename one or pick a more specific value"
        )
    return matches[0]


def _resolve_at_start_of_file(text: str, anchor: AnchorAtStartOfFile) -> int:
    """Return line offset 0 (start of file).

    ``text`` and ``anchor`` are accepted for shape symmetry with the
    other resolvers; both are unused.
    """
    del text, anchor
    return 0


def _resolve_at_end_of_file(text: str, anchor: AnchorAtEndOfFile) -> int:
    """Return line offset == number of lines (one past the last line)."""
    del anchor
    if not text:
        return 0
    return len(text.splitlines())


def _find_after_section_offsets(text: str, name: str) -> list[int]:
    """Return every 0-indexed line offset immediately after a user-section
    end marker whose key equals ``name`` (setforge-xsco).

    Routes through :func:`setforge.sections._walk_markers` so the scan
    inherits the strict parser's validation (nested sections,
    end-without-start, etc.). End-marker key matching uses the
    canonical ``key`` (named sections by name; unnamed by string index).
    """
    matches: list[int] = []
    for line_count, event in enumerate(_walk_markers(text, allow_legacy=True), start=1):
        if isinstance(event, _EndMarker) and event.key == name:
            matches.append(line_count)
    return matches


def _resolve_after_section(text: str, anchor: AnchorAfterSection) -> int:
    """Return the line offset immediately after the named section's end marker."""
    matches = _find_after_section_offsets(text, anchor.name)
    if not matches:
        raise AnchorNotFoundError(
            f"no user-section matched anchor after-section {anchor.name!r}"
        )
    if len(matches) > 1:
        lines_1 = ", ".join(str(m) for m in matches)
        raise AnchorAmbiguousError(
            f"anchor after-section {anchor.name!r} matches multiple "
            f"sections ending at lines {lines_1}"
        )
    return matches[0]


def resolve_anchor(text: str, anchor: Anchor) -> int:
    """Return the 0-indexed line offset in ``text`` where ``anchor`` resolves.

    Dispatches on the anchor's discriminated-union shape. ``text`` is
    EOL-normalised before the scan so a CRLF live file matches the same
    headings as the LF tracked source. Raises :class:`AnchorNotFoundError`
    when the anchor matches nothing and :class:`AnchorAmbiguousError`
    when it matches more than one candidate.
    """
    normalised = _normalise_eol(text)
    match anchor:
        case AnchorAfterHeading():
            return _resolve_after_heading(normalised, anchor)
        case AnchorBeforeHeading():
            return _resolve_before_heading(normalised, anchor)
        case AnchorAtStartOfFile():
            return _resolve_at_start_of_file(normalised, anchor)
        case AnchorAtEndOfFile():
            return _resolve_at_end_of_file(normalised, anchor)
        case AnchorAfterSection():
            return _resolve_after_section(normalised, anchor)
        case _ as never:
            # Exhaustiveness guard: adding a 6th anchor variant to the
            # discriminated union without extending this match fails at
            # type-check time (mypy / pyright surface ``never``'s
            # narrowed type as the unhandled variant).
            assert_never(never)


def _read_body(section: HostLocalSection) -> str:
    """Return the section's body content from inline ``body`` or ``body_file``.

    Pydantic's :meth:`HostLocalSection._exactly_one_body_source`
    guarantees exactly one is set, so this is a discriminator-style
    pick with no fallthrough.
    """
    if section.body is not None:
        return section.body
    assert section.body_file is not None  # exactly-one-of guarantee
    return section.body_file.read_text(encoding="utf-8")


def inject_host_local_section(
    text: str,
    name: str,
    anchor: Anchor,
    body: str,
) -> str:
    """Splice a marker pair + body into ``text`` at the resolved anchor.

    EOL-normalises ``text`` first (anti-smell item 11 — CRLF live, LF
    tracked). Builds the marker pair via
    :func:`setforge.cli.section._format_marker_pair_unstamped` so the
    canonical layout is owned by ONE module, then routes the whole
    result through :func:`setforge.cli.section._stamp_section_hashes`
    to satisfy the post-install hash invariant. The section semantics
    keyword is always ``host-local`` (anti-smell item 8).

    Idempotency check (anti-smell item 15): if a section named ``name``
    already exists in ``text``, raise :class:`AnchorAmbiguousError` —
    the caller must NOT re-inject. The install path is responsible for
    pre-flighting against the LIVE file's section name set so a re-run
    of ``setforge install`` updates the body in place rather than
    appending a duplicate pair (handled in :func:`inject_all`).
    """
    # Lazy import: setforge.cli.section -> setforge.compare ->
    # setforge.host_local_inject would form a module-import cycle if
    # this hoist were at the top. The cli.section module is imported on
    # first call (after compare's import-time graph has settled). Mirrors
    # the cycle-breaking pattern at setforge/config.py:587
    # (apply_preserve_user_keys_overlay).
    from setforge.cli.section import (
        _format_marker_pair_unstamped,
        _stamp_section_hashes,
    )

    normalised = _normalise_eol(text)
    line_offset = resolve_anchor(normalised, anchor)
    pair = _format_marker_pair_unstamped(
        semantics=SectionSemantics.HOST_LOCAL.value, name=name, body=body
    )
    lines = normalised.splitlines(keepends=True)
    head = "".join(lines[:line_offset])
    tail = "".join(lines[line_offset:])
    # Ensure the head ends in a newline so the marker pair lands on its
    # own line. The only way head can lack a trailing newline is when
    # the splice point is at_end_of_file on a file that does not end
    # with "\n"; in that case we add the newline so the marker pair is
    # well-formed.
    if head and not head.endswith("\n"):
        head += "\n"
    return _stamp_section_hashes(head + pair + tail)


def inject_all(
    text: str,
    sections: dict[str, HostLocalSection],
) -> str:
    """Inject every section in ``sections`` into ``text`` in declaration order.

    Idempotency: when ``text`` already contains a user-section whose
    name matches a key in ``sections``, the existing section's BODY
    is replaced in place (no new marker pair is spliced). Anchors are
    only consulted for first-injection (when the section is absent
    from ``text``).

    The whole returned text is finally routed through
    :func:`_stamp_section_hashes` so every end marker carries the
    canonical ``hash=<sha256-hex>`` segment for both newly-spliced and
    body-replaced sections.

    Routes through :func:`setforge.sections.extract_sections` for the
    presence check so the install path sees the same section-name set
    the rest of the engine reads from the file. Raises every error
    :func:`inject_host_local_section` raises (anchor not found, anchor
    ambiguous).
    """
    # Lazy import: same cycle-break rationale as inject_host_local_section
    # (cli.section -> compare -> host_local_inject); _stamp_section_hashes
    # is the proximate dependency.
    from setforge.cli.section import _stamp_section_hashes
    from setforge.sections import extract_sections, merge_sections

    result = _normalise_eol(text)
    existing = extract_sections(result, allow_legacy=True)
    new_bodies: dict[str, str] = {}
    for section_name, section in sections.items():
        body = _read_body(section)
        if section_name in existing:
            new_bodies[section_name] = body
            continue
        result = inject_host_local_section(result, section_name, section.anchor, body)
    if new_bodies:
        # Re-extract because inject_host_local_section may have added
        # new pairs that are not present in the original ``existing``.
        all_sections = extract_sections(result, allow_legacy=True)
        all_sections.update(new_bodies)
        result = merge_sections(result, all_sections)
        result = _stamp_section_hashes(result)
    return result
