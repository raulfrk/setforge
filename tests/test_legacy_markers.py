"""Tests for the frozen legacy user-section marker reader.

``setforge._legacy_markers`` is the frozen home for the marker-READING
machinery retained after schema-2.1 marker retirement: the migration chain
(``_contract_2_0``, ``host_local_marker_migration``) and
``host_local_inject``'s ``after-section`` anchor resolver still parse legacy
markers, and ``capture`` / ``migrate`` / ``validate`` still strip them. The
marker EMIT/HASH machinery did NOT move here — it is deleted with
``sections.py``.
"""

import pytest

from setforge._legacy_markers import (
    SectionSemantics,
    _EndMarker,
    _walk_markers,
    detect_duplicate_section_names,
    detect_legacy_markers,
    detect_legacy_namespace_markers,
    extract_sections,
    section_semantics,
    strip_host_local_markers,
    strip_host_local_sections,
    strip_shared_markers,
)
from setforge.errors import MarkerError

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


def test_section_semantics_classifies() -> None:
    assert section_semantics(_HOST_LOCAL_DOC) == {"NAME": SectionSemantics.HOST_LOCAL}
    assert section_semantics(_SHARED_DOC) == {"S": SectionSemantics.SHARED}


def test_strip_shared_markers_keeps_body_drops_lines() -> None:
    out = strip_shared_markers(_SHARED_DOC)
    assert "setforge:user-section" not in out
    assert "shared body" in out


def test_strip_host_local_markers_removes_pair_and_body() -> None:
    out = strip_host_local_markers(_HOST_LOCAL_DOC)
    assert "setforge:user-section" not in out
    assert "live body" not in out


def test_strip_host_local_sections_is_name_scoped() -> None:
    out = strip_host_local_sections(_HOST_LOCAL_DOC, names=frozenset({"NAME"}))
    assert "live body" not in out
    assert "setforge:user-section" not in out


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
    assert section_semantics(legacy, allow_legacy=True) == {
        "0": SectionSemantics.SHARED
    }


def test_detect_legacy_namespace_markers_flags_my_setup() -> None:
    assert detect_legacy_namespace_markers(
        "<!-- my-setup:user-section start shared S -->\n"
    )
    assert not detect_legacy_namespace_markers(_SHARED_DOC)


def test_detect_legacy_markers_flags_missing_hash() -> None:
    no_hash = (
        "<!-- setforge:user-section start shared S -->\n"
        "x\n"
        "<!-- setforge:user-section end shared S -->\n"
    )
    assert detect_legacy_markers(no_hash)


def test_detect_duplicate_section_names_reports_repeat() -> None:
    dup = _SHARED_DOC + _SHARED_DOC
    assert detect_duplicate_section_names(dup) == "S"


def test_walk_markers_yields_end_marker_event() -> None:
    ends = [e for e in _walk_markers(_HOST_LOCAL_DOC) if isinstance(e, _EndMarker)]
    assert len(ends) == 1
    assert ends[0].semantics is SectionSemantics.HOST_LOCAL
    assert ends[0].key == "NAME"
