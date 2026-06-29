"""Unit tests for the structured (YAML/JSON/JSONC) per-key staging model
(:mod:`setforge.reconcile.structured_units`).

The structured analog of :mod:`setforge.reconcile.hunks`: instead of line-hunks
it extracts per-KEY units with a path-only identity (a dotted leaf path),
classified by the same :class:`~setforge.reconcile.types.HunkClass`, reconstructed
through the model (never text substitution) so comments/anchors/quoting survive.
"""

from __future__ import annotations

import pytest

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


# Round-trip fidelity: a no-promotion reconstruct (empty unit set) must equal the
# input byte-for-byte. Each case pins one structured-parser smell (SP1/5/6/7).
_YAML_ROUNDTRIP = [
    pytest.param(b"flag: yes\nother: no\n", id="bool-like-yes-no"),  # SP1
    pytest.param(b"ver: !!str 5\n", id="str-tag"),  # SP1
    pytest.param(b"ports: [80, 443]\nhost: a\n", id="flow-seq"),  # SP5
    pytest.param(b"map: {a: 1, b: 2}\n", id="flow-map"),  # SP5
    pytest.param(b"q1: 'single'\nq2: \"double\"\n", id="quote-styles"),  # SP1
    pytest.param(b"a: &x 1\nb: *x\n", id="anchor-alias"),  # SP3
    pytest.param(b"# lead\nk: 1  # trail\n# tail\n", id="comments"),  # SP6
    pytest.param(b"port: 1\nport_str: '1'\nflt: 1.0\n", id="int-vs-str-vs-float"),
]


@pytest.mark.parametrize("text", _YAML_ROUNDTRIP)
def test_reconstruct_yaml_roundtrip_byte_identical_no_promotion(text: bytes) -> None:
    """No promotion → pure load+dump must reproduce the input verbatim (SP1/5/6)."""
    out = reconstruct_structured(text, text, [], {}, StructuredFormat.YAML)
    assert out == text


_JSONC_ROUNDTRIP = [
    pytest.param(
        b'{\n  // a comment\n  "a": 1,\n  "b": 2,\n}\n', id="comment+trailing-comma"
    ),
    pytest.param(
        b'{\n  "nested": {\n    "x": true,\n  },\n}\n', id="nested-trailing-comma"
    ),
]


@pytest.mark.parametrize("text", _JSONC_ROUNDTRIP)
def test_reconstruct_jsonc_roundtrip_byte_identical_no_promotion(text: bytes) -> None:
    """JSONC comments + trailing commas survive a no-promotion round-trip (SP7)."""
    out = reconstruct_structured(text, text, [], {}, StructuredFormat.JSONC)
    assert out == text
