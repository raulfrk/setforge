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

from my_setup.errors import MarkerError

LOGGER = logging.getLogger(__name__)

_MARKER_RE = re.compile(
    r"^\s*<!--\s*my-setup:user-section\s+(start|end)"
    r"(?:\s+(?!hash=)(\S+))?"
    r"(?:\s+hash=([0-9a-f]{64}))?"
    r"\s*-->\s*$"
)


def extract_sections(text: str) -> dict[str, str]:
    """Return the content between every marker pair in ``text``.

    Named sections are keyed by their name; unnamed sections are keyed by
    sequential string indices ("0", "1", ...). Section content includes any
    trailing newline up to (but not including) the end-marker line.

    Raises :class:`MarkerError` for nested sections, end-without-start,
    name-mismatched pairs, or unclosed start markers.
    """
    sections: dict[str, str] = {}
    in_section = False
    section_name: str | None = None
    section_lines: list[str] = []
    unnamed_index = 0

    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        match = _MARKER_RE.match(line)
        if match is None:
            if in_section:
                section_lines.append(line)
            continue
        kind, name = match.group(1), match.group(2)
        if kind == "start":
            if in_section:
                raise MarkerError(
                    f"line {lineno}: nested user-section start "
                    f"(previous section still open)"
                )
            in_section = True
            section_name = name
            section_lines = []
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
            sections[key] = "".join(section_lines)
            if section_name is None:
                unnamed_index += 1
            in_section = False
            section_name = None
            section_lines = []

    if in_section:
        identifier = section_name if section_name is not None else str(unnamed_index)
        raise MarkerError(f"unclosed user-section (started as {identifier!r})")

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
    in_section = False
    section_name: str | None = None
    placeholder_lines: list[str] = []
    unnamed_index = 0
    consumed: set[str] = set()

    for line in tracked_text.splitlines(keepends=True):
        match = _MARKER_RE.match(line)
        if match is None:
            if in_section:
                placeholder_lines.append(line)
            else:
                out_lines.append(line)
            continue
        kind, name = match.group(1), match.group(2)
        if kind == "start":
            out_lines.append(line)
            in_section = True
            section_name = name
            placeholder_lines = []
        else:
            key = section_name if section_name is not None else str(unnamed_index)
            if key in live_sections:
                content = live_sections[key]
                if content and not content.endswith("\n"):
                    content += "\n"
                out_lines.append(content)
                consumed.add(key)
            else:
                out_lines.extend(placeholder_lines)
            out_lines.append(line)
            if section_name is None:
                unnamed_index += 1
            in_section = False
            section_name = None
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
