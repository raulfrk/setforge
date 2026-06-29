"""Unit tests for the marker-retire migration's frozen embedded parser.

The self-contained marker parser the migration embeds so it keeps working AFTER
``sections.py`` is deleted. Its correctness is data-loss-critical (it decides
host-local vs shared, which gates what reaches the shared repo), so it is
**differential-tested** against the trusted :mod:`setforge.sections` parser for
valid input + pinned on the security-critical strict-refuse cases.
"""

from __future__ import annotations

import pytest

from setforge import sections
from setforge.errors import MarkerError
from setforge.migrations._marker_retire import ParsedSection, parse_markers

_SHARED = (
    "intro line\n"
    "<!-- setforge:user-section start shared rules -->\n"
    "Use rg not grep.\n"
    "<!-- setforge:user-section end shared rules "
    "hash=" + "a" * 64 + " -->\n"
    "middle line\n"
    "<!-- setforge:user-section start host-local paths -->\n"
    "workdir: /home/raul\n"
    "<!-- setforge:user-section end host-local paths "
    "hash=" + "b" * 64 + " -->\n"
    "tail line\n"
)


def test_embedded_parser_matches_sections_for_valid_input() -> None:
    """The embedded parser is byte-equivalent to sections.* for valid markers."""
    parsed = {p.name: p for p in parse_markers(_SHARED)}

    assert {n: p.semantics for n, p in parsed.items()} == {
        n: s.value for n, s in sections.section_semantics(_SHARED).items()
    }
    assert {n: p.body for n, p in parsed.items()} == sections.extract_sections(_SHARED)
    assert {
        n: p.embedded_hash for n, p in parsed.items()
    } == sections.extract_marker_hashes(_SHARED)


def test_embedded_parser_classifies_semantics() -> None:
    """Each section carries its host-local/shared keyword verbatim."""
    parsed = {p.name: p for p in parse_markers(_SHARED)}
    assert parsed["rules"].semantics == "shared"
    assert parsed["paths"].semantics == "host-local"
    assert parsed["paths"].body == "workdir: /home/raul\n"


def test_embedded_parser_returns_parsedsection_instances() -> None:
    """The public type is ParsedSection (name/semantics/body/embedded_hash)."""
    out = parse_markers(_SHARED)
    assert all(isinstance(p, ParsedSection) for p in out)
    assert [p.name for p in out] == ["rules", "paths"]


def test_embedded_parser_refuses_missing_keyword() -> None:
    """A marker missing the host-local|shared keyword is REFUSED (never defaulted
    to SHARED — the host-local-leak trap, fail-closed)."""
    bad = (
        "<!-- setforge:user-section start secrets -->\n"
        "token: abc\n"
        "<!-- setforge:user-section end secrets hash=" + "c" * 64 + " -->\n"
    )
    with pytest.raises(MarkerError):
        parse_markers(bad)


def test_embedded_parser_refuses_legacy_namespace() -> None:
    """A pre-rename ``my-setup:`` marker is REFUSED (would otherwise be invisible
    and silently lose its host-local body)."""
    legacy = (
        "<!-- my-setup:user-section start host-local paths -->\n"
        "workdir: /home/raul\n"
        "<!-- my-setup:user-section end host-local paths -->\n"
    )
    with pytest.raises(MarkerError):
        parse_markers(legacy)


def test_embedded_parser_no_markers_yields_empty() -> None:
    """A file with no markers parses to no sections (never raises)."""
    assert parse_markers("just\nplain\ntext\n") == []


def test_embedded_parser_empty_body_section_kept() -> None:
    """An empty-body section round-trips as an empty string, not dropped (MP-7)."""
    text = (
        "<!-- setforge:user-section start host-local blank -->\n"
        "<!-- setforge:user-section end host-local blank hash=" + "d" * 64 + " -->\n"
    )
    parsed = {p.name: p for p in parse_markers(text)}
    assert "blank" in parsed
    assert parsed["blank"].body == ""
