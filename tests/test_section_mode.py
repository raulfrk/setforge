"""Tests for the leaf-enum module ``setforge.section_mode``."""

from setforge.section_mode import SectionMode


def test_section_mode_members() -> None:
    assert SectionMode.KEEP_DEFAULTS == "keep_defaults"
    assert SectionMode.STRIP == "strip"
