"""Tests for the reconcile-native user-section marker reader.

``setforge.user_section_markers`` is the live, non-legacy home for the
marker-READING machinery the current engine still needs after the schema-2.1
marker retirement — a byte-faithful port of the grammar frozen in
``setforge._legacy_markers``. It backs the five live consumers:
``host_local_inject`` (after-section anchors), ``cli.compare``
(``extract_sections``), ``cli.validate`` (``contains_user_section_marker``),
``cli.migrate`` (``strip_host_local_markers``), and ``cli._helpers``
(``detect_duplicate_section_names``). These tests pin the ported surface so
the live reader stays behaviourally identical to the frozen one.
"""

import pytest

from setforge.errors import MarkerError
from setforge.user_section_markers import (
    SectionSemantics,
    _EndMarker,
    _walk_markers,
    contains_user_section_marker,
    detect_duplicate_section_names,
    extract_sections,
    strip_host_local_markers,
    strip_host_local_sections,
)

_HOST_LOCAL_DOC = (
    "before\n"
    "<!-- setforge:user-section start host-local NAME -->\n"
    "live body\n"
    f"<!-- setforge:user-section end host-local NAME hash={'a' * 64} -->\n"
    "after\n"
)
_SHARED_DOC = (
    "head\n"
    "<!-- setforge:user-section start shared S -->\n"
    "shared body\n"
    f"<!-- setforge:user-section end shared S hash={'b' * 64} -->\n"
    "tail\n"
)


def test_extract_sections_reads_marker_body() -> None:
    assert extract_sections(_HOST_LOCAL_DOC) == {"NAME": "live body\n"}


def test_extract_sections_rejects_duplicate_name() -> None:
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'S'"):
        extract_sections(_SHARED_DOC + _SHARED_DOC)


def test_strip_host_local_markers_removes_pair_and_body() -> None:
    out = strip_host_local_markers(_HOST_LOCAL_DOC)
    assert "setforge:user-section" not in out
    assert "live body" not in out


def test_strip_host_local_sections_is_name_scoped() -> None:
    # Named pair removed; a non-listed name would pass through unchanged.
    out = strip_host_local_sections(_HOST_LOCAL_DOC, names=frozenset({"NAME"}))
    assert "live body" not in out
    assert "setforge:user-section" not in out


def test_strip_host_local_sections_noop_on_empty_names() -> None:
    assert strip_host_local_sections(_HOST_LOCAL_DOC, names=frozenset()) == (
        _HOST_LOCAL_DOC
    )


def test_strip_host_local_markers_keeps_shared_pair() -> None:
    out = strip_host_local_markers(_SHARED_DOC)
    assert "setforge:user-section" in out
    assert "shared body" in out


def test_strict_parse_refuses_missing_semantics_keyword() -> None:
    bad = (
        "<!-- setforge:user-section start -->\nx\n<!-- setforge:user-section end -->\n"
    )
    with pytest.raises(MarkerError):
        extract_sections(bad)


def test_allow_legacy_tolerates_missing_keyword_as_shared() -> None:
    legacy = (
        "<!-- setforge:user-section start -->\nx\n<!-- setforge:user-section end -->\n"
    )
    assert extract_sections(legacy, allow_legacy=True) == {"0": "x\n"}


def test_detect_duplicate_section_names_reports_repeat() -> None:
    assert detect_duplicate_section_names(_SHARED_DOC + _SHARED_DOC) == "S"


def test_detect_duplicate_section_names_none_when_distinct() -> None:
    assert detect_duplicate_section_names(_HOST_LOCAL_DOC + _SHARED_DOC) is None


def test_contains_user_section_marker_detects_start_and_end() -> None:
    assert contains_user_section_marker(_HOST_LOCAL_DOC)
    assert contains_user_section_marker(
        "<!-- setforge:user-section start shared S -->\n"
    )
    assert contains_user_section_marker("<!-- setforge:user-section end -->\n")


def test_contains_user_section_marker_ignores_prose_mention() -> None:
    prose = "The `setforge:user-section` marker pairs preserve host edits.\n"
    assert not contains_user_section_marker(prose)
    assert not contains_user_section_marker("plain content\nno markers here\n")


def test_walk_markers_yields_end_marker_event() -> None:
    ends = [e for e in _walk_markers(_HOST_LOCAL_DOC) if isinstance(e, _EndMarker)]
    assert len(ends) == 1
    assert ends[0].semantics is SectionSemantics.HOST_LOCAL
    assert ends[0].key == "NAME"
