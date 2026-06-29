"""Unit tests for the structured (YAML/JSON/JSONC) per-key staging model
(:mod:`setforge.reconcile.structured_units`).

The structured analog of :mod:`setforge.reconcile.hunks`: instead of line-hunks
it extracts per-KEY units with a path-only identity (a dotted leaf path),
classified by the same :class:`~setforge.reconcile.types.HunkClass`, reconstructed
through the model (never text substitution) so comments/anchors/quoting survive.
"""

from __future__ import annotations

import pytest

from setforge.errors import InvariantViolation
from setforge.reconcile.structured_units import (
    KeyUnit,
    StructuredFormat,
    assert_stage_fidelity_structured,
    classify_structured,
    extract_structured_units,
    reconstruct_structured,
    serialize_structured,
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


def _row(cls: str, path: str, value_hash: str) -> dict[str, object]:
    return {
        "kind": "key",
        "cls": cls,
        "label": path,
        "path": path,
        "value_hash": value_hash,
    }


def test_classify_carries_stored_class_by_path() -> None:
    """A fresh unit whose path+value_hash match a stored row inherits its class."""
    fresh = [KeyUnit(HunkClass.PENDING, "fontSize", "fontSize", "sha256:v16")]
    stored = [_row("shared", "fontSize", "sha256:v16")]

    out = classify_structured(fresh, stored)

    assert out[0].cls is HunkClass.SHARED
    assert out[0].changed is False


def test_classify_value_edit_keeps_class_but_flags_changed() -> None:
    """A path match with a CHANGED value keeps the class, flagged for re-confirm."""
    fresh = [KeyUnit(HunkClass.PENDING, "fontSize", "fontSize", "sha256:v18")]
    stored = [_row("shared", "fontSize", "sha256:v16")]

    out = classify_structured(fresh, stored)

    assert out[0].cls is HunkClass.SHARED
    assert out[0].changed is True


def test_classify_unmatched_path_stays_pending() -> None:
    """A path with no stored row stays PENDING (the extract default)."""
    fresh = [KeyUnit(HunkClass.PENDING, "newKey", "newKey", "sha256:x")]

    out = classify_structured(fresh, [])

    assert out[0].cls is HunkClass.PENDING


def test_assert_stage_fidelity_structured_passes_when_tracked_matches() -> None:
    """INV-8 holds: tracked == reconstruct of the promoted set."""
    base = b"a: 1\nb: 2\n"
    live = b"a: 9\nb: 2\n"
    units = [KeyUnit(HunkClass.SHARED, "a", "a", "sha256:x")]
    tracked = reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)

    assert_stage_fidelity_structured(
        base, live, tracked, units, {}, StructuredFormat.YAML
    )


def test_assert_stage_fidelity_structured_raises_when_tracked_carries_local() -> None:
    """INV-8 violated: tracked holds a LOCAL key's live value → raise."""
    base = b"a: 1\nb: 2\n"
    live = b"a: 9\nb: 2\n"
    units = [KeyUnit(HunkClass.LOCAL, "a", "a", "sha256:x")]
    tracked = b"a: 9\nb: 2\n"  # carries the host-local edit it must NOT

    with pytest.raises(InvariantViolation):
        assert_stage_fidelity_structured(
            base, live, tracked, units, {}, StructuredFormat.YAML
        )


def test_serialize_structured_emits_key_kind_rows() -> None:
    """A key-unit projects to a kind:'key' index row carrying its path+value_hash."""
    units = [
        KeyUnit(HunkClass.SHARED, "editor.fontSize", "editor.fontSize", "sha256:v")
    ]

    rows = serialize_structured(units)

    assert rows == [
        {
            "kind": "key",
            "cls": "shared",
            "label": "editor.fontSize",
            "path": "editor.fontSize",
            "value_hash": "sha256:v",
        }
    ]


def test_serialize_structured_drafted_carries_draft_hash() -> None:
    """A SHARED_DRAFTED key-unit additionally serialises its draft_hash."""
    units = [
        KeyUnit(HunkClass.SHARED_DRAFTED, "k", "k", "sha256:v", draft_hash="sha256:d")
    ]

    rows = serialize_structured(units)

    assert rows[0]["kind"] == "key"
    assert rows[0]["draft_hash"] == "sha256:d"


def test_reconstruct_shared_drafted_splices_draft_value() -> None:
    """A SHARED_DRAFTED unit takes its DRAFT value (not live, not base)."""
    base = b"path: /home/alice/x\n"
    live = b"path: /home/alice/x\n"
    units = [
        KeyUnit(
            HunkClass.SHARED_DRAFTED, "path", "path", "sha256:v", draft_hash="sha256:d"
        )
    ]
    drafts = {"path": b"/home/USER/x"}

    out = reconstruct_structured(base, live, units, drafts, StructuredFormat.YAML)

    assert out == b"path: /home/USER/x\n"


def test_reconstruct_shared_drafted_missing_draft_raises() -> None:
    """A dangling draft pointer fails closed — never falls back to live/base."""
    units = [
        KeyUnit(
            HunkClass.SHARED_DRAFTED, "path", "path", "sha256:v", draft_hash="sha256:d"
        )
    ]

    with pytest.raises(InvariantViolation):
        reconstruct_structured(
            b"path: /h\n", b"path: /h\n", units, {}, StructuredFormat.YAML
        )


def test_extract_mapping_key_reorder_mints_no_phantom_unit() -> None:
    """A pure key reorder changes no leaf path → zero units (path-by-name id)."""
    base = b"a: 1\nb: 2\nc: 3\n"
    live = b"c: 3\na: 1\nb: 2\n"

    units = extract_structured_units(base, live, StructuredFormat.YAML)

    assert units == []
