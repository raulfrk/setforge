"""Readers for the retired user-section markers that live verbs still need.

The marker grammar itself lives in :mod:`setforge.migrations._frozen_markers`
(the copy the old-schema migrations read through). This module adds the
readers that copy lacks:

* :mod:`setforge.cli.validate` gates tracked sources on residual markers
  (:func:`contains_user_section_marker`);
* :mod:`setforge.cli.migrate` de-markers host-local pairs
  (:func:`strip_host_local_markers`);
* :mod:`setforge.capture` name-scoped strips host-local pairs on writeback
  (:func:`strip_host_local_sections`).

:func:`extract_sections` is re-exported for :mod:`setforge.cli.compare`.
"""

from __future__ import annotations

from typing import assert_never

from setforge.migrations._frozen_markers import (
    _MARKER_PREFIX_RE,
    SectionSemantics,
    _BodyLine,
    _EndMarker,
    _OutsideLine,
    _StartMarker,
    _walk_markers,
    extract_sections,
)

__all__ = [
    "contains_user_section_marker",
    "extract_sections",
    "strip_host_local_markers",
    "strip_host_local_sections",
]


def contains_user_section_marker(text: str) -> bool:
    """Return ``True`` if ``text`` contains any ``setforge:user-section`` marker line.

    Regex-only scan over the broad marker-prefix detector — matches a start
    OR end marker line regardless of whether it is well-formed, pre-hash, or
    otherwise malformed. A prose MENTION of the marker syntax (text that is
    not itself a complete ``<!-- ... -->`` marker line) does not match.

    Used by ``setforge validate`` to enforce that tracked sources carry no
    residual markers after the schema-2.1 retirement migration.
    """
    return any(_MARKER_PREFIX_RE.match(line) for line in text.splitlines())


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
    Shared marker pairs always pass through. ``allow_legacy`` defaults to
    ``True`` (lenient) — unlike the strict :func:`extract_sections` default
    (``False``) — because capture reads live-side text that may still
    contain pre-hash markers.

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
            case _EndMarker(semantics=SectionSemantics.HOST_LOCAL, name=name) if (
                name in names
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


def strip_host_local_markers(text: str, *, allow_legacy: bool = True) -> str:
    """Return ``text`` with EVERY host-local marker pair (markers + body) removed.

    The deploy-side de-marker for the markerless host-local migration: once a
    host-local section's body is carried as a markerless overlay (injected
    after the merge), the tracked-authored placeholder pair must not reach the
    deployed file. Drops every host-local user-section pair regardless of
    name; shared marker pairs always pass through untouched.

    The whole file is parsed via :func:`_walk_markers` (which validates
    pairing and raises :class:`MarkerError` on any malformed / unclosed /
    nested / mismatched marker), so the function returns the complete
    stripped string or raises — never a partial result. The kept lines are
    exact-bytes: no normalization, no trailing-newline policy.

    Idempotent: once the host-local pairs are gone, a second pass finds none
    and returns its input unchanged.
    """
    out_lines: list[str] = []
    drop = False
    for event in _walk_markers(text, allow_legacy=allow_legacy):
        match event:
            case _StartMarker(semantics=SectionSemantics.HOST_LOCAL):
                drop = True
                continue
            case _EndMarker(semantics=SectionSemantics.HOST_LOCAL):
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
