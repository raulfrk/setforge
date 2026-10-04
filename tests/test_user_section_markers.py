"""Tests for the readers of the retired user-section markers.

``setforge.user_section_markers`` holds what the live verbs still need on top
of the grammar frozen in ``setforge.migrations._frozen_markers``:
``cli.validate`` (``contains_user_section_marker``) and ``cli.migrate``
(``strip_host_local_markers``).
"""

from setforge.migrations._frozen_markers import (
    SectionSemantics,
    _EndMarker,
    _walk_markers,
    extract_sections,
)
from setforge.user_section_markers import (
    contains_user_section_marker,
    strip_host_local_markers,
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


def test_strip_host_local_markers_removes_pair_and_body() -> None:
    out = strip_host_local_markers(_HOST_LOCAL_DOC)
    assert "setforge:user-section" not in out
    assert "live body" not in out


def test_strip_host_local_markers_keeps_shared_pair() -> None:
    out = strip_host_local_markers(_SHARED_DOC)
    assert "setforge:user-section" in out
    assert "shared body" in out


def test_allow_legacy_tolerates_missing_keyword_as_shared() -> None:
    legacy = (
        "<!-- setforge:user-section start -->\nx\n<!-- setforge:user-section end -->\n"
    )
    assert extract_sections(legacy, allow_legacy=True) == {"0": "x\n"}


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
