"""Unit tests for the structured (YAML/JSON/JSONC) per-key staging model
(:mod:`setforge.reconcile.structured_units`).

The structured analog of :mod:`setforge.reconcile.hunks`: instead of line-hunks
it extracts per-KEY units with a path-only identity (a dotted leaf path),
classified by the same :class:`~setforge.reconcile.types.HunkClass`, reconstructed
through the model (never text substitution) so comments/anchors/quoting survive.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import pytest

from setforge.errors import (
    DraftConfinementError,
    InvariantViolation,
    StructuredParseError,
)
from setforge.reconcile.structured_units import (
    KeyUnit,
    StructuredFormat,
    _load_model,
    _walk_leaves,
    assert_stage_fidelity_structured,
    classify_structured,
    extract_structured_units,
    parse_scalar_draft,
    reconstruct_structured,
    serialize_structured,
)
from setforge.reconcile.types import HunkClass, UnitRef


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


def test_extract_identical_jsonc_mints_no_unit() -> None:
    document = b'{\n  // retained comment\n  "enabled": true\n}\n'

    units = extract_structured_units(document, document, StructuredFormat.JSONC)

    assert units == []


def test_extract_yaml_scalar_type_change_mints_unit() -> None:
    units = extract_structured_units(
        b"enabled: 1\n", b"enabled: true\n", StructuredFormat.YAML
    )

    assert [unit.path for unit in units] == ["enabled"]


def test_reconstruct_shared_nested_add_materializes_only_promoted_path() -> None:
    base = b"existing: keep\n"
    live = b"existing: keep\nnew:\n  nested:\n    shared: 1\n    local: 2\n"
    fresh = extract_structured_units(base, live, StructuredFormat.YAML)
    assert [unit.path for unit in fresh] == ["new.nested.local", "new.nested.shared"]
    units = [
        replace(
            unit,
            cls=(
                HunkClass.SHARED
                if unit.path == "new.nested.shared"
                else HunkClass.LOCAL
            ),
        )
        for unit in fresh
    ]

    try:
        out = reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)
    except KeyError:
        out = None

    assert out == b"existing: keep\nnew:\n  nested:\n    shared: 1\n"


@pytest.mark.parametrize(
    ("base", "live", "paths"),
    [
        pytest.param(
            b"existing: keep\n",
            b"existing: keep\nempty: {}\n",
            ["empty"],
            id="add",
        ),
        pytest.param(
            b"existing: keep\nempty: {}\n",
            b"existing: keep\n",
            ["empty"],
            id="delete",
        ),
        pytest.param(b"{}\n", b"a: 1\n", ["a"], id="empty-root-to-key"),
        pytest.param(b"a: 1\n", b"{}\n", ["a"], id="key-to-empty-root"),
    ],
)
def test_reconstruct_shared_empty_mapping_round_trips_exactly(
    base: bytes, live: bytes, paths: list[str]
) -> None:
    units = extract_structured_units(base, live, StructuredFormat.YAML)

    assert [unit.path for unit in units] == paths
    promoted = [replace(unit, cls=HunkClass.SHARED) for unit in units]
    assert (
        reconstruct_structured(base, live, promoted, {}, StructuredFormat.YAML) == live
    )


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


def test_reconstruct_promoted_shared_deleted_leaf_drops_key_yaml() -> None:
    """A promoted SHARED unit whose live value is ABSENT (the key was deleted
    live) DROPS the leaf from base — mirroring the line path's empty-span
    deletion — instead of splicing the ABSENT sentinel, which used to crash
    _dump_model and leave the file permanently uncapturable."""
    base = b"# top\na: 1\nb: 2  # keep me\n"
    live = b"# top\nb: 2  # keep me\n"  # key 'a' deleted live
    units = [KeyUnit(HunkClass.SHARED, "a", "a", "sha256:x")]

    out = reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)

    assert b"a: 1" not in out  # the deleted leaf is gone
    assert b"b: 2  # keep me" in out  # sibling + its inline comment survive


def test_reconstruct_promoted_shared_deleted_leaf_drops_key_jsonc() -> None:
    """The JSONC backend drops a promoted-SHARED deleted leaf, sibling intact."""
    import json

    base = b'{\n  "a": 1,\n  "b": 2\n}\n'
    live = b'{\n  "b": 2\n}\n'  # key 'a' deleted live
    units = [KeyUnit(HunkClass.SHARED, "a", "a", "sha256:x")]

    out = reconstruct_structured(base, live, units, {}, StructuredFormat.JSONC)

    # Re-parse to pin key/value integrity (a non-lockstep delete would desync
    # keys/values and corrupt the object), not just byte-substrings.
    assert json.loads(out) == {"b": 2}


def test_reconstruct_promoted_shared_absent_in_both_fails_closed() -> None:
    """A promoted SHARED unit whose path resolves in NEITHER base nor live
    fails closed (StructuredParseError) rather than silently no-op — the
    INV-8 residual guard. Pins the else-branch directly (the pre-existing
    non-string-key test reaches it only incidentally)."""
    base = b"a: 1\nb: 2\n"
    live = b"a: 1\nb: 2\n"
    units = [KeyUnit(HunkClass.SHARED, "ghost", "ghost", "sha256:x")]

    with pytest.raises(StructuredParseError):
        reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)


def test_reconstruct_all_shared_parent_replacement_subsumes_child_deletion() -> None:
    """A mapping-to-scalar replacement is one coherent promoted shape change.

    Extraction represents it as a new parent leaf plus deletion of the old child
    leaf.  Applying the parent first makes the child absent from the working base;
    that deletion is already satisfied and must not be mistaken for a malformed
    unit.
    """
    base = b"a:\n  b: 1\n"
    live = b"a: 2\n"
    units = [
        replace(unit, cls=HunkClass.SHARED)
        for unit in extract_structured_units(base, live, StructuredFormat.YAML)
    ]

    out = reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)

    assert out == live


@pytest.mark.parametrize(
    ("base", "live"),
    [
        pytest.param(
            b"a:\n  b:\n    c: 1\n",
            b"a: 2\n",
            id="mapping-to-scalar",
        ),
        pytest.param(
            b"a: 1\n",
            b"a:\n  b:\n    c: 2\n",
            id="scalar-to-mapping",
        ),
    ],
)
def test_reconstruct_all_shared_shape_change_is_order_independent(
    base: bytes, live: bytes
) -> None:
    """Ancestors apply first even when callers supply deeper units first."""
    units = [
        replace(unit, cls=HunkClass.SHARED)
        for unit in extract_structured_units(base, live, StructuredFormat.YAML)
    ]

    forward = reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)
    reverse = reconstruct_structured(
        base, live, list(reversed(units)), {}, StructuredFormat.YAML
    )

    assert forward == reverse == live


@pytest.mark.parametrize("fmt", [StructuredFormat.YAML, StructuredFormat.JSONC])
@pytest.mark.parametrize(
    ("base", "live"),
    [
        pytest.param(b"1\n", b"2\n", id="scalar-to-scalar"),
        pytest.param(b"a: 1\n", b"2\n", id="mapping-to-scalar-yaml"),
        pytest.param(b"1\n", b"a:\n  b: 2\n", id="scalar-to-mapping-yaml"),
    ],
)
def test_reconstruct_shared_document_root_replacement_is_live_exact(
    fmt: StructuredFormat, base: bytes, live: bytes
) -> None:
    if fmt is StructuredFormat.JSONC:
        base = b'{"a": 1}\n' if base == b"a: 1\n" else base
        live = b'{"a": {"b": 2}}\n' if live == b"a:\n  b: 2\n" else live
    units = [
        replace(unit, cls=HunkClass.SHARED)
        for unit in extract_structured_units(base, live, fmt)
    ]
    assert any(unit.path == "" for unit in units)

    assert reconstruct_structured(base, live, units, {}, fmt) == live
    assert reconstruct_structured(base, live, list(reversed(units)), {}, fmt) == live


def test_reconstruct_root_and_deeper_mixed_intent_fails_closed() -> None:
    fmt = StructuredFormat.YAML
    base = b"0\n"
    live = b"a:\n  b: 2\n"
    units = [
        replace(
            unit,
            cls=HunkClass.SHARED if unit.path == "" else HunkClass.LOCAL,
        )
        for unit in extract_structured_units(base, live, fmt)
    ]

    with pytest.raises(StructuredParseError, match=r"incompatible.*intent"):
        reconstruct_structured(base, live, units, {}, fmt)


def test_jsonc_mapping_change_stays_one_opaque_root_unit() -> None:
    """JSON/JSONC staging remains whole-document, never recursive key walking."""
    base = b'{"a": 1, "untouched": true}\n'
    live = b'{"a": 2, "untouched": true}\n'

    units = extract_structured_units(base, live, StructuredFormat.JSONC)

    assert [unit.path for unit in units] == [""]
    promoted = [replace(units[0], cls=HunkClass.SHARED)]
    assert (
        reconstruct_structured(base, live, promoted, {}, StructuredFormat.JSONC) == live
    )


@pytest.mark.parametrize(
    ("classes"),
    [
        pytest.param(
            {"a": HunkClass.SHARED, "a.b": HunkClass.LOCAL},
            id="shared-parent-local-child",
        ),
        pytest.param(
            {"a": HunkClass.LOCAL, "a.b": HunkClass.SHARED},
            id="local-parent-shared-child",
        ),
    ],
)
def test_reconstruct_rejects_incompatible_parent_descendant_intent(
    classes: dict[str, HunkClass],
) -> None:
    """Overlapping units cannot independently keep and promote the same shape."""
    base = b"a:\n  b: 1\n"
    live = b"a: 2\n"
    units = [
        replace(unit, cls=classes[unit.path])
        for unit in extract_structured_units(base, live, StructuredFormat.YAML)
    ]

    with pytest.raises(StructuredParseError, match=r"incompatible.*intent"):
        reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)


@pytest.mark.parametrize(
    ("base", "live", "drafted_path"),
    [
        pytest.param(
            b"a:\n  b: 1\n",
            b"a: 2\n",
            "a",
            id="drafted-scalar-parent",
        ),
        pytest.param(
            b"a: 1\n",
            b"a:\n  b: 2\n",
            "a.b",
            id="drafted-scalar-descendant",
        ),
    ],
)
def test_reconstruct_rejects_overlapping_drafted_and_shared_shape_change(
    base: bytes, live: bytes, drafted_path: str
) -> None:
    """A scalar draft cannot safely compose with a promoted structural peer."""
    units = [
        replace(
            unit,
            cls=(
                HunkClass.SHARED_DRAFTED
                if unit.path == drafted_path
                else HunkClass.SHARED
            ),
            draft_hash=("sha256:d" if unit.path == drafted_path else None),
        )
        for unit in extract_structured_units(base, live, StructuredFormat.YAML)
    ]

    with pytest.raises(StructuredParseError, match=r"drafted.*overlap"):
        reconstruct_structured(
            base,
            live,
            list(reversed(units)),
            {UnitRef.key(drafted_path): b"3"},
            StructuredFormat.YAML,
        )


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
    assert out[0].confirmed_hash == "sha256:v16"
    assert serialize_structured(out)[0]["value_hash"] == "sha256:v16"


def test_classify_serialize_changed_shared_stays_changed_on_second_run() -> None:
    stored = [_row("shared", "fontSize", "sha256:v16")]
    second = classify_structured(
        [KeyUnit(HunkClass.PENDING, "fontSize", "fontSize", "sha256:v18")],
        stored,
    )
    assert second[0].changed is True

    persisted_again = serialize_structured(second)
    third = classify_structured(
        [KeyUnit(HunkClass.PENDING, "fontSize", "fontSize", "sha256:v18")],
        persisted_again,
    )

    assert third[0].cls is HunkClass.SHARED
    assert third[0].changed is True
    assert persisted_again[0]["value_hash"] == "sha256:v16"


def test_classify_unmatched_path_stays_pending() -> None:
    """A path with no stored row stays PENDING (the extract default)."""
    fresh = [KeyUnit(HunkClass.PENDING, "newKey", "newKey", "sha256:x")]

    out = classify_structured(fresh, [])

    assert out[0].cls is HunkClass.PENDING


def test_classify_rejects_duplicate_stored_key_paths() -> None:
    fresh = [KeyUnit(HunkClass.PENDING, "same", "same", "sha256:fresh")]
    with pytest.raises(
        InvariantViolation, match="duplicate stored structured key path"
    ):
        classify_structured(
            fresh,
            [
                _row("shared", "same", "sha256:one"),
                _row("local", "same", "sha256:two"),
            ],
        )


def test_classify_rejects_duplicate_fresh_key_paths() -> None:
    fresh = [
        KeyUnit(HunkClass.PENDING, "one", "same", "sha256:one"),
        KeyUnit(HunkClass.PENDING, "two", "same", "sha256:two"),
    ]
    with pytest.raises(InvariantViolation, match="duplicate fresh structured key path"):
        classify_structured(fresh, [])


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
    drafts = {UnitRef.key("path"): b"/home/USER/x"}

    out = reconstruct_structured(base, live, units, drafts, StructuredFormat.YAML)

    assert out == b"path: /home/USER/x\n"


def test_reconstruct_shared_drafted_preserves_scalar_type() -> None:
    """A drafted value splices with its PARSED type (int stays int, not a string)."""
    base = b"port: 8080\n"
    live = b"port: 8080\n"
    units = [
        KeyUnit(
            HunkClass.SHARED_DRAFTED, "port", "port", "sha256:v", draft_hash="sha256:d"
        )
    ]
    drafts = {UnitRef.key("port"): b"22"}

    out = reconstruct_structured(base, live, units, drafts, StructuredFormat.YAML)

    # int 22 dumps unquoted; a string "22" would round-trip as `port: '22'`.
    assert out == b"port: 22\n"


def test_reconstruct_shared_drafted_rejects_structure_injection() -> None:
    """A draft-store value that parses to a MAP is refused at splice (fail-closed)."""
    base = b"path: /h\n"
    live = b"path: /h\n"
    units = [
        KeyUnit(
            HunkClass.SHARED_DRAFTED, "path", "path", "sha256:v", draft_hash="sha256:d"
        )
    ]
    drafts = {UnitRef.key("path"): b"injected: evil"}

    with pytest.raises(DraftConfinementError):
        reconstruct_structured(base, live, units, drafts, StructuredFormat.YAML)


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


def test_reconstruct_long_scalar_not_reflowed() -> None:
    """A >80-column scalar round-trips without a width-wrap phantom diff (SP5)."""
    long_val = "x" * 120
    text = f"url: {long_val}\n".encode()

    out = reconstruct_structured(text, text, [], {}, StructuredFormat.YAML)

    assert out == text


def test_parse_scalar_draft_yaml_plain_string_keeps_str_type() -> None:
    """A generalized path draft parses to a plain ``str`` (the common case)."""
    assert parse_scalar_draft(b"~/.config", StructuredFormat.YAML) == "~/.config"
    assert isinstance(parse_scalar_draft(b"~/.config", StructuredFormat.YAML), str)


def test_parse_scalar_draft_yaml_int_preserves_int_type() -> None:
    """A numeric draft parses to ``int`` — not the string ``"8730"`` (SP type id)."""
    value = parse_scalar_draft(b"8730", StructuredFormat.YAML)
    assert value == 8730
    assert isinstance(value, int)
    assert not isinstance(value, bool)


def test_parse_scalar_draft_yaml_bool_preserves_bool_type() -> None:
    """``true`` parses to ``bool`` (kept distinct from int 1 by structural_merge)."""
    value = parse_scalar_draft(b"true", StructuredFormat.YAML)
    assert value is True


def test_parse_scalar_draft_jsonc_int_preserves_int_type() -> None:
    """The JSONC backend parses a numeric draft to ``int`` too."""
    value = parse_scalar_draft(b"42", StructuredFormat.JSONC)
    assert value == 42
    assert isinstance(value, int)
    assert not isinstance(value, bool)


def test_parse_scalar_draft_rejects_mapping() -> None:
    """A draft that parses to a MAP injects siblings/nesting → confinement error."""
    with pytest.raises(DraftConfinementError):
        parse_scalar_draft(b"a: b", StructuredFormat.YAML)


def test_parse_scalar_draft_rejects_multikey_mapping_sibling_injection() -> None:
    """A multi-key map (sibling injection) is rejected (not a single scalar)."""
    with pytest.raises(DraftConfinementError):
        parse_scalar_draft(b"value: x\ninjected: y", StructuredFormat.YAML)


def test_parse_scalar_draft_rejects_list() -> None:
    """A draft that parses to a LIST is not a scalar → confinement error."""
    with pytest.raises(DraftConfinementError):
        parse_scalar_draft(b"[1, 2, 3]", StructuredFormat.YAML)


def test_parse_scalar_draft_rejects_yaml_anchor() -> None:
    """A YAML anchor (``&``) is bounded out even though it wraps a scalar."""
    with pytest.raises(DraftConfinementError):
        parse_scalar_draft(b"&a 0", StructuredFormat.YAML)


def test_parse_scalar_draft_rejects_yaml_merge_key() -> None:
    """A ``<<`` merge key parses as a mapping with an alias → rejected."""
    with pytest.raises(DraftConfinementError):
        parse_scalar_draft(b"<<: x", StructuredFormat.YAML)


def test_parse_scalar_draft_rejects_control_char() -> None:
    """A forbidden control char in the draft is rejected (untrusted-output gate)."""
    with pytest.raises(DraftConfinementError):
        parse_scalar_draft(b"ok\x00bad", StructuredFormat.YAML)


def test_parse_scalar_draft_rejects_jsonc_object() -> None:
    """A JSONC object draft is structure injection → confinement error."""
    with pytest.raises(DraftConfinementError):
        parse_scalar_draft(b'{"a": 1}', StructuredFormat.JSONC)


def test_parse_scalar_draft_rejects_python_object_tag() -> None:
    """A ``!!python/...`` tag is scalar-shaped but refused by the safe loader."""
    with pytest.raises(DraftConfinementError):
        parse_scalar_draft(b"!!python/name:os.system ''", StructuredFormat.YAML)


def test_parse_scalar_draft_rejects_unconstructable_tag() -> None:
    """A scalar-shaped but ill-typed tag (``!!timestamp notadate``) is refused."""
    with pytest.raises(DraftConfinementError):
        parse_scalar_draft(b"!!timestamp notadate", StructuredFormat.YAML)


def test_extract_merge_key_does_not_surface_inherited_keys_as_units() -> None:
    """A ``<<`` merge key must not surface inherited keys as own units (SP4).

    Changing the anchored ``defaults.x`` also changes ``child``'s INHERITED view
    of ``x``; only ``defaults.x`` is a real own leaf, so a phantom ``child.x``
    unit must NOT appear.
    """
    base = b"defaults: &d\n  x: 1\nchild:\n  <<: *d\n  y: 2\n"
    live = b"defaults: &d\n  x: 9\nchild:\n  <<: *d\n  y: 2\n"

    units = extract_structured_units(base, live, StructuredFormat.YAML)

    assert [u.path for u in units] == ["defaults.x"]


def test_multidoc_yaml_raises_never_silently_truncates() -> None:
    """A multi-document YAML must FAIL rather than silently drop documents 2+ (SP2).

    The guarantee at this layer is no silent single-doc truncation: multi-doc
    *fails closed*, it does not round-trip. A byte-identical multi-doc round-trip
    would require ``load_all``, which design smell SP2 ('multi-doc load_all')
    explicitly forbids, so failing closed is the correct resolution rather than
    a regression — silently parsing only document 1 would drop documents 2+.
    ruamel's round-trip loader raises on a multi-document stream; the line-level
    fallback for an unparseable structured file is the stage walk's concern.
    """
    text = b"a: 1\n---\nb: 2\n"

    with pytest.raises(StructuredParseError):
        reconstruct_structured(text, text, [], {}, StructuredFormat.YAML)


def test_non_utf8_bytes_raise_structured_parse_error() -> None:
    """Non-UTF-8 structured bytes fail closed as StructuredParseError at decode.

    ``_load_model`` decodes as UTF-8 first; an invalid byte sequence must surface
    as the typed :class:`StructuredParseError`, not a raw ``UnicodeDecodeError``.
    """
    bad = b"\xff\xfe: 1\n"

    with pytest.raises(StructuredParseError):
        extract_structured_units(bad, bad, StructuredFormat.YAML)


def test_classify_ignores_line_rows_in_mixed_stored() -> None:
    """A line-row sharing the entry is ignored; the key-unit still classifies (SP).

    ``classify_structured`` filters ``stored`` to ``kind:"key"`` rows, so a
    line-hunk row (``anchor`` + ``live_hash``, no ``path``) can never raise an
    unwrapped ``KeyError`` while the real key-unit still inherits its class.
    """
    fresh = [KeyUnit(HunkClass.PENDING, "fontSize", "fontSize", "sha256:v16")]
    line_row: dict[str, object] = {
        "kind": "line",
        "cls": "shared",
        "label": "some line",
        "live_hash": "sha256:l",
        "anchor": "sha256:a",
    }
    stored = [line_row, _row("shared", "fontSize", "sha256:v16")]

    out = classify_structured(fresh, stored)

    assert out[0].cls is HunkClass.SHARED
    assert out[0].changed is False


def test_reconstruct_merge_key_roundtrip_byte_identical() -> None:
    """A YAML doc using a ``<<`` merge key round-trips byte-identical (SP4).

    The extract side is pinned by test_extract_merge_key_...; this pins the
    RECONSTRUCT side: a no-promotion rebuild of a merge-key doc reproduces the
    input verbatim, so the ``<<`` reference and its anchor survive load+dump.
    """
    text = b"defaults: &d\n  x: 1\nchild:\n  <<: *d\n  y: 2\n"

    out = reconstruct_structured(text, text, [], {}, StructuredFormat.YAML)

    assert out == text


def test_reconstruct_changed_shared_holds_at_base() -> None:
    """A ``changed`` SHARED unit is held at BASE, not promoted to live.

    ``_promotes`` returns ``False`` for a ``changed`` SHARED unit (value edited
    since it was staged), so reconstruct keeps its base value pending re-confirm.
    """
    base = b"a: 1\nb: 2\n"
    live = b"a: 9\nb: 2\n"
    units = [KeyUnit(HunkClass.SHARED, "a", "a", "sha256:x", changed=True)]

    out = reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)

    assert out == base  # held at base a:1, not promoted to live a:9


def test_classify_shared_drafted_matches_by_path_ignoring_value_hash() -> None:
    """A SHARED_DRAFTED row matches by PATH alone, carrying its draft_hash.

    Its tracked bytes come from the draft store, so the live ``value_hash`` may
    differ from the stored one; the unit still classifies SHARED_DRAFTED,
    ``changed=False``, with the stored ``draft_hash`` carried through.
    """
    fresh = [KeyUnit(HunkClass.PENDING, "path", "path", "sha256:vNEW")]
    stored: list[dict[str, object]] = [
        {
            "kind": "key",
            "cls": "shared_drafted",
            "label": "path",
            "path": "path",
            "value_hash": "sha256:vOLD",
            "draft_hash": "sha256:d",
        }
    ]

    out = classify_structured(fresh, stored)

    assert out[0].cls is HunkClass.SHARED_DRAFTED
    assert out[0].changed is False
    assert out[0].draft_hash == "sha256:d"


# --- dotted-key collision (injective path encoding) ----------------------------


def test_flat_dotted_key_distinct_from_nested_key() -> None:
    """A literal flat key ``a.b`` mints a unit DISTINCT from nested ``a -> b``.

    Regression for the path-collision bug: ``_walk_leaves`` joined segments with a
    bare ``.``, so a flat key literally named ``"a.b"`` and a nested ``a: {b: …}``
    both produced the dotted path ``"a.b"`` and ``dict(_walk_leaves(...))`` silently
    collapsed the two physically-distinct leaves to one unit. An injective encoding
    keeps them separate.
    """
    base = b'"a.b": 0\na:\n  b: 0\n'
    live = b'"a.b": 9\na:\n  b: 5\n'

    units = extract_structured_units(base, live, StructuredFormat.YAML)

    assert len(units) == 2
    assert len({u.path for u in units}) == 2


def test_walk_leaves_root_empty_key_distinct_from_no_prefix() -> None:
    """An empty-string ROOT key must not collide with the no-prefix root.

    Regression for the empty-string-key collision: ``_walk_leaves`` used
    empty-string truthiness (``not prefix``) to mean "root", conflating the root
    with a GENUINE empty-string mapping key. A real ``None`` sentinel for "root"
    keeps the two distinct, so two physically-distinct leaves never collapse in
    the ``dict(_walk_leaves(...))`` the extractor builds.
    """
    # An empty-string key nesting ``{"a": 1}`` must not collapse onto a bare
    # ``{"a": 1}`` (the empty key contributes a real, distinct segment).
    nested_empty = list(_walk_leaves({"": {"a": 1}}))
    plain = list(_walk_leaves({"a": 1}))
    assert nested_empty != plain
    assert {p for p, _ in nested_empty}.isdisjoint({p for p, _ in plain})

    # ``{"": 1}`` and ``{"": {"": 9}}`` must yield distinct leaf paths.
    scalar_empty = {p for p, _ in _walk_leaves({"": 1})}
    double_empty = {p for p, _ in _walk_leaves({"": {"": 9}})}
    assert scalar_empty != double_empty


def test_extract_empty_string_key_distinct_from_nonempty_same_shape() -> None:
    """A doc with BOTH an empty-string key and a same-shaped non-empty key mints
    two DISTINCT units (no silent collapse from the root/empty-key conflation)."""
    base = b'"":\n  a: 0\na: 0\n'
    live = b'"":\n  a: 9\na: 9\n'

    units = extract_structured_units(base, live, StructuredFormat.YAML)

    assert len(units) == 2
    assert len({u.path for u in units}) == 2


@pytest.mark.parametrize(
    ("base", "live", "expected"),
    [
        pytest.param(b'"": old\nkeep: 1\n', b'"": new\nkeep: 1\n', "new", id="update"),
        pytest.param(b'"": old\nkeep: 1\n', b"keep: 1\n", None, id="delete"),
    ],
)
def test_yaml_empty_key_uses_non_root_path_and_reconstructs(
    base: bytes, live: bytes, expected: str | None
) -> None:
    units = extract_structured_units(base, live, StructuredFormat.YAML)
    assert [unit.path for unit in units] == [r"\0"]

    out = reconstruct_structured(
        base,
        live,
        [replace(units[0], cls=HunkClass.SHARED)],
        {},
        StructuredFormat.YAML,
    )
    model = cast("Mapping[str, object]", _load_model(out, StructuredFormat.YAML))
    if expected is None:
        assert "" not in model
    else:
        assert model[""] == expected


def test_legacy_yaml_empty_path_row_still_addresses_empty_key() -> None:
    """A mapping↔mapping legacy ``path:''`` remains an empty-key identity."""
    base = b'"": old\nkeep: 1\n'
    live = b'"": new\nkeep: 1\n'
    legacy = KeyUnit(HunkClass.SHARED, "", "", "sha256:legacy")

    out = reconstruct_structured(base, live, [legacy], {}, StructuredFormat.YAML)

    model = cast("Mapping[str, object]", _load_model(out, StructuredFormat.YAML))
    assert model[""] == "new"


def test_reconstruct_promotes_flat_dotted_key_to_flat_slot() -> None:
    """A promoted flat ``a.b`` unit round-trips to the FLAT key, not nested ``a -> b``.

    The injective encoding must survive the reconstruct round-trip: ``set_node_at_path``
    has to place the promoted value at the literal ``"a.b"`` key, not descend into a
    phantom ``a -> b`` nesting.
    """
    base = b'"a.b": 0\n'
    live = b'"a.b": 9\n'

    units = extract_structured_units(base, live, StructuredFormat.YAML)
    assert len(units) == 1

    promoted = [replace(u, cls=HunkClass.SHARED) for u in units]
    out = reconstruct_structured(base, live, promoted, {}, StructuredFormat.YAML)

    model = cast("Mapping[str, object]", _load_model(out, StructuredFormat.YAML))
    assert model["a.b"] == 9
    assert "a" not in model  # no phantom nested key minted


def test_nested_nondotted_key_row_survives_reclassify_unchanged() -> None:
    """A non-dotted nested key encodes IDENTICALLY → a stored SHARED row still matches.

    Backward-compat guard: the injective encoding only diverges for keys with
    a literal ``.`` (or the escape char); an ordinary nested key like
    ``editor.fontSize`` keeps its byte-identical dotted path, so a row persisted
    before the fix is not re-minted (it re-classifies SHARED, not PENDING).
    """
    base = b"editor:\n  fontSize: 14\n"
    live = b"editor:\n  fontSize: 16\n"

    units = extract_structured_units(base, live, StructuredFormat.YAML)
    stored = serialize_structured([replace(u, cls=HunkClass.SHARED) for u in units])
    fresh = extract_structured_units(base, live, StructuredFormat.YAML)

    classified = classify_structured(fresh, stored)

    assert [u.path for u in classified] == ["editor.fontSize"]
    assert all(u.cls is HunkClass.SHARED for u in classified)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(b"1: alpha\nname: beta\n", id="int-key"),
        pytest.param(b"true: alpha\nname: beta\n", id="bool-key"),
        pytest.param(b"2020-01-01: alpha\nname: beta\n", id="date-key"),
    ],
)
def test_extract_non_string_mapping_key_fails_closed(text: bytes) -> None:
    with pytest.raises(StructuredParseError):
        extract_structured_units(text, text, StructuredFormat.YAML)


def test_inv8_non_string_key_unreachable_via_extract() -> None:
    """The non-string-key crash is now structurally excluded upstream,
    not merely caught."""
    base = b"1: alpha\nname: beta\n"
    live = b"1: alpha\nname: gamma\n"

    with pytest.raises(StructuredParseError):
        extract_structured_units(base, live, StructuredFormat.YAML)


def test_inv8_residual_reconstruct_splice_fails_closed() -> None:
    base = b"1: alpha\nname: beta\n"
    live = b"1: zeta\nname: beta\n"
    units = [KeyUnit(HunkClass.SHARED, "1", "1", "sha256:x")]

    with pytest.raises(StructuredParseError):
        reconstruct_structured(base, live, units, {}, StructuredFormat.YAML)


def test_serialize_structured_never_emits_reloc_anchor() -> None:
    """A structured key-unit row is markdown-line-free: ``reloc_anchor`` (a heading
    identity) is line-hunk-only and must NEVER appear on a ``kind:"key"`` row, nor
    be part of the KEY-row codec's required identity.

    Locks the surface split: a key-unit's identity is ``path`` + ``value_hash``, not
    the line model's heading-relocation anchor. The check runs across a LOCAL and a
    SHARED_DRAFTED unit (the two classes that could plausibly carry extra keys) and
    confirms each serialized row (a) omits ``reloc_anchor`` and (b) validates through
    the fail-closed index codec as a KEY row without it.
    """
    from setforge.reconcile.index_model import (
        _HUNK_ROW_KEYS_KEY,
        KIND_KEY,
        _check_hunk_row,
    )

    units = [
        KeyUnit(cls=HunkClass.LOCAL, label="a.b", path="a.b", value_hash="sha256:aa"),
        KeyUnit(
            cls=HunkClass.SHARED_DRAFTED,
            label="c",
            path="c",
            value_hash="sha256:cc",
            draft_hash="sha256:dd",
        ),
    ]

    rows = serialize_structured(units)

    assert all("reloc_anchor" not in row for row in rows)
    assert "reloc_anchor" not in _HUNK_ROW_KEYS_KEY
    for row in rows:
        row["kind"] = KIND_KEY
        _check_hunk_row("f", row)
