"""Tests for the leaf-enum module ``setforge.section_mode``."""

from setforge.section_mode import ReconcileAuto, SectionMode


def test_section_mode_members() -> None:
    assert SectionMode.KEEP_DEFAULTS == "keep_defaults"
    assert SectionMode.STRIP == "strip"


def test_reconcile_auto_members() -> None:
    # ReconcileAuto lives here (relocated from the deleted section_wizard) so
    # the disposition/deploy engine can dispatch on it without importing a
    # legacy marker module.
    assert ReconcileAuto.USE_TRACKED == "use-tracked"
    assert ReconcileAuto.KEEP_LIVE == "keep-live"
    assert {m.value for m in ReconcileAuto} == {"use-tracked", "keep-live"}
