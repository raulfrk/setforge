"""Unit tests for the structured (YAML/JSON/JSONC) per-key staging model
(:mod:`setforge.reconcile.structured_units`).

The structured analog of :mod:`setforge.reconcile.hunks`: instead of line-hunks
it extracts per-KEY units with a path-only identity (a dotted leaf path),
classified by the same :class:`~setforge.reconcile.types.HunkClass`, reconstructed
through the model (never text substitution) so comments/anchors/quoting survive.
"""

from __future__ import annotations

from setforge.reconcile.structured_units import (
    KeyUnit,
    StructuredFormat,
    extract_structured_units,
    reconstruct_structured,
)
from setforge.reconcile.types import HunkClass


def test_extract_one_changed_scalar_leaf_yields_one_pending_unit() -> None:
    """A single changed scalar leaf → exactly one PENDING key-unit at that path."""
    base = b"theme: dark\nfontSize: 14\n"
    live = b"theme: dark\nfontSize: 16\n"

    units = extract_structured_units(base, live, StructuredFormat.YAML)

    assert len(units) == 1
    unit = units[0]
    assert unit.path == "fontSize"
    assert unit.cls is HunkClass.PENDING


def test_extract_nested_key_yields_dotted_path_unchanged_sibling_mints_nothing() -> (
    None
):
    """A nested changed leaf → a dotted path; an unchanged sibling mints no unit."""
    base = b"editor:\n  fontSize: 14\n  theme: dark\n"
    live = b"editor:\n  fontSize: 16\n  theme: dark\n"

    units = extract_structured_units(base, live, StructuredFormat.YAML)

    assert [u.path for u in units] == ["editor.fontSize"]


def test_reconstruct_all_local_returns_base_byte_identical() -> None:
    """Promoting nothing → base verbatim: comments/quotes/inline survive (SP5/SP6)."""
    base = b'# top\ntheme: "dark"\nfontSize: 14  # inline note\n'
    live = b'# top\ntheme: "dark"\nfontSize: 16  # inline note\n'
    units = [
        KeyUnit(
            cls=HunkClass.LOCAL,
            label="fontSize",
            path="fontSize",
            value_hash="sha256:x",
        )
    ]

    out = reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)

    assert out == base


def test_reconstruct_promotes_shared_leaf_preserving_siblings() -> None:
    """A SHARED leaf takes its live value; every other byte is untouched."""
    base = b'# top\ntheme: "dark"\nfontSize: 14  # inline note\n'
    live = b'# top\ntheme: "dark"\nfontSize: 16  # inline note\n'
    units = [
        KeyUnit(
            cls=HunkClass.SHARED,
            label="fontSize",
            path="fontSize",
            value_hash="sha256:x",
        )
    ]

    out = reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)

    assert out == b'# top\ntheme: "dark"\nfontSize: 16  # inline note\n'
