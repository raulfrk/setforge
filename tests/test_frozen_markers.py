"""Grammar tests for the migration chain's frozen marker reader.

:mod:`setforge.migrations._frozen_markers` is the author-time byte-faithful
freeze of the legacy user-section marker parser the migration chain still
reads through (``_contract_2_0`` enumerates marked sections;
``host_local_marker_migration`` walks live markers). Its correctness is
data-loss-critical — it classifies host-local vs shared, which gates what
reaches the shared repo — so the grammar edge cases the frozen copy MUST
honor are pinned here. The marker EMIT / HASH machinery (``merge_sections`` /
``set_marker_hashes`` / ``hash_sections`` / ``extract_marker_hashes``) did not
survive retirement and is not covered.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from setforge.errors import MarkerError
from setforge.migrations import _frozen_markers as frozen
from setforge.migrations._frozen_markers import (
    SectionSemantics,
    _EndMarker,
    _walk_markers,
    extract_sections,
    section_semantics,
)

_HASH_HEX_64: str = "a" * 64


# ---------------------------------------------------------------------------
# extract_sections — basic parse + keying
# ---------------------------------------------------------------------------


def test_no_markers_passthrough() -> None:
    assert extract_sections("line 1\nline 2\nline 3\n") == {}


def test_single_unnamed_section_extract() -> None:
    text = (
        "before\n"
        "<!-- setforge:user-section start host-local -->\n"
        "preserved 1\npreserved 2\n"
        "<!-- setforge:user-section end host-local -->\n"
        "after\n"
    )
    assert extract_sections(text, allow_legacy=True) == {
        "0": "preserved 1\npreserved 2\n"
    }


def test_extract_named_sections_keyed_by_name() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "wf\n"
        "<!-- setforge:user-section end shared workflow -->\n"
        "<!-- setforge:user-section start shared commits -->\n"
        "cm\n"
        "<!-- setforge:user-section end shared commits -->\n"
    )
    assert extract_sections(text, allow_legacy=True) == {
        "workflow": "wf\n",
        "commits": "cm\n",
    }


def test_extract_unnamed_indices_in_order() -> None:
    text = (
        "<!-- setforge:user-section start host-local -->\n"
        "first\n"
        "<!-- setforge:user-section end host-local -->\n"
        "<!-- setforge:user-section start host-local -->\n"
        "second\n"
        "<!-- setforge:user-section end host-local -->\n"
    )
    assert extract_sections(text, allow_legacy=True) == {
        "0": "first\n",
        "1": "second\n",
    }


# ---------------------------------------------------------------------------
# extract_sections — pairing / structural errors
# ---------------------------------------------------------------------------


def test_mismatched_missing_end_raises() -> None:
    text = "<!-- setforge:user-section start host-local -->\ncontent\n"
    with pytest.raises(MarkerError, match="unclosed"):
        extract_sections(text)


def test_end_without_start_raises() -> None:
    text = "<!-- setforge:user-section end host-local -->\n"
    with pytest.raises(MarkerError, match="without matching start"):
        extract_sections(text)


def test_name_mismatch_raises() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "<!-- setforge:user-section end shared commits -->\n"
    )
    with pytest.raises(MarkerError, match="does not match"):
        extract_sections(text)


def test_nested_section_raises() -> None:
    text = (
        "<!-- setforge:user-section start shared outer -->\n"
        "<!-- setforge:user-section start shared inner -->\n"
        "<!-- setforge:user-section end shared inner -->\n"
        "<!-- setforge:user-section end shared outer -->\n"
    )
    with pytest.raises(MarkerError, match="nested"):
        extract_sections(text)


# ---------------------------------------------------------------------------
# hash= segment parsing on end markers
# ---------------------------------------------------------------------------


def test_extract_sections_parses_end_marker_with_hash() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared a hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"a": "body\n"}


def test_extract_sections_parses_unnamed_end_marker_with_hash() -> None:
    text = (
        "<!-- setforge:user-section start host-local -->\n"
        "body\n"
        f"<!-- setforge:user-section end host-local hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"0": "body\n"}


def test_extract_sections_hashless_end_marker_under_allow_legacy_parses() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "body\n"
        "<!-- setforge:user-section end shared a -->\n"
    )
    assert extract_sections(text, allow_legacy=True) == {"a": "body\n"}


def test_malformed_hash_segment_raises_in_strict_mode() -> None:
    text = (
        "<!-- setforge:user-section start shared FOO -->\n"
        "body\n"
        "<!-- setforge:user-section end shared FOO hash=NOTHEX -->\n"
    )
    with pytest.raises(MarkerError, match="malformed hash"):
        extract_sections(text)


def test_malformed_hash_segment_treated_as_absent_under_allow_legacy() -> None:
    text = (
        "<!-- setforge:user-section start shared FOO -->\n"
        "body\n"
        "<!-- setforge:user-section end shared FOO hash=NOTHEX -->\n"
    )
    assert extract_sections(text, allow_legacy=True) == {"FOO": "body\n"}


def test_valid_64_hex_hash_still_parses() -> None:
    valid = "a" * 64
    text = (
        "<!-- setforge:user-section start shared FOO -->\n"
        "body\n"
        f"<!-- setforge:user-section end shared FOO hash={valid} -->\n"
    )
    assert extract_sections(text) == {"FOO": "body\n"}


# ---------------------------------------------------------------------------
# Required host-local|shared keyword + malformed keyword
# ---------------------------------------------------------------------------


def test_untagged_start_marker_raises_marker_error() -> None:
    text = (
        "<!-- setforge:user-section start workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end shared workflow -->\n"
    )
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(text)


def test_untagged_unnamed_start_raises_marker_error() -> None:
    text = (
        "<!-- setforge:user-section start -->\n"
        "body\n"
        "<!-- setforge:user-section end -->\n"
    )
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(text)


def test_semantics_mismatch_raises_marker_error() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end host-local workflow -->\n"
    )
    with pytest.raises(MarkerError, match="end semantics"):
        extract_sections(text)


def test_unknown_semantics_keyword_raises_at_parse_time() -> None:
    text = (
        "<!-- setforge:user-section start unknown workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end shared workflow -->\n"
    )
    with pytest.raises(MarkerError, match="unknown semantics keyword 'unknown'"):
        extract_sections(text)


def test_unknown_semantics_raises_with_line_context() -> None:
    text = (
        "<!-- setforge:user-section start fish-tacos NAME -->\n"
        "body\n"
        "<!-- setforge:user-section end fish-tacos NAME -->\n"
    )
    with pytest.raises(MarkerError) as excinfo:
        extract_sections(text)
    msg = str(excinfo.value)
    assert "line 1" in msg
    assert "unknown semantics" in msg
    assert "fish-tacos" in msg


def test_unknown_semantics_raises_under_allow_legacy() -> None:
    # Only NULL/missing semantics gets the legacy SHARED fallback; an
    # explicit-but-invalid keyword is still a malformed marker.
    text = (
        "<!-- setforge:user-section start fish-tacos NAME -->\n"
        "body\n"
        "<!-- setforge:user-section end fish-tacos NAME -->\n"
    )
    with pytest.raises(MarkerError, match="unknown semantics"):
        extract_sections(text, allow_legacy=True)


def test_hash_in_semantics_position_raises_with_missing_semantics_hint() -> None:
    text = (
        "<!-- setforge:user-section start hash=abc workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end shared workflow -->\n"
    )
    with pytest.raises(
        MarkerError, match="is missing the semantics keyword before 'hash="
    ):
        extract_sections(text)


def test_hash_in_semantics_position_on_end_marker_raises() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "body\n"
        "<!-- setforge:user-section end hash=abc workflow -->\n"
    )
    with pytest.raises(
        MarkerError, match="is missing the semantics keyword before 'hash="
    ):
        extract_sections(text)


def test_extract_sections_accepts_host_local_keyword() -> None:
    text = (
        "<!-- setforge:user-section start host-local notes -->\n"
        "host-local body\n"
        f"<!-- setforge:user-section end host-local notes hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"notes": "host-local body\n"}


def test_extract_sections_accepts_shared_keyword() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "shared body\n"
        f"<!-- setforge:user-section end shared workflow hash={_HASH_HEX_64} -->\n"
    )
    assert extract_sections(text) == {"workflow": "shared body\n"}


# ---------------------------------------------------------------------------
# section_semantics
# ---------------------------------------------------------------------------


def test_section_semantics_returns_keyword_per_section() -> None:
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "wf\n"
        "<!-- setforge:user-section end shared workflow -->\n"
        "<!-- setforge:user-section start host-local notes -->\n"
        "notes\n"
        "<!-- setforge:user-section end host-local notes -->\n"
    )
    assert section_semantics(text, allow_legacy=True) == {
        "workflow": "shared",
        "notes": "host-local",
    }


def test_section_semantics_coverage_parity_with_extract_sections() -> None:
    text = (
        "<!-- setforge:user-section start shared a -->\n"
        "alpha\n"
        "<!-- setforge:user-section end shared a -->\n"
        "<!-- setforge:user-section start host-local b -->\n"
        "beta\n"
        "<!-- setforge:user-section end host-local b -->\n"
    )
    assert (
        section_semantics(text, allow_legacy=True).keys()
        == extract_sections(text, allow_legacy=True).keys()
    )


def test_section_semantics_unnamed_keying_mirrors_extract_sections() -> None:
    text = (
        "<!-- setforge:user-section start host-local -->\n"
        "first\n"
        "<!-- setforge:user-section end host-local -->\n"
        "<!-- setforge:user-section start shared -->\n"
        "second\n"
        "<!-- setforge:user-section end shared -->\n"
    )
    assert section_semantics(text, allow_legacy=True) == {
        "0": "host-local",
        "1": "shared",
    }


def test_section_semantics_propagates_marker_error() -> None:
    text = "<!-- setforge:user-section start workflow -->\nbody\n"
    with pytest.raises(MarkerError):
        section_semantics(text)


def test_section_semantics_value_is_canonical_string() -> None:
    """Values are :class:`SectionSemantics` members; since it is a StrEnum,
    they compare equal to and are instances of ``str``."""
    text = (
        "<!-- setforge:user-section start shared workflow -->\n"
        "wf\n"
        "<!-- setforge:user-section end shared workflow -->\n"
    )
    value = section_semantics(text, allow_legacy=True)["workflow"]
    assert value is SectionSemantics.SHARED
    assert isinstance(value, str)  # StrEnum is-a str
    assert value == "shared"


# ---------------------------------------------------------------------------
# Duplicate-name rejection (the dict-keyed primitives fail loudly)
# ---------------------------------------------------------------------------


_TWO_SHARED_A = (
    "<!-- setforge:user-section start shared A -->\n"
    "LIVE1\n"
    f"<!-- setforge:user-section end shared A hash={'0' * 64} -->\n"
    "<!-- setforge:user-section start shared A -->\n"
    "LIVE2\n"
    f"<!-- setforge:user-section end shared A hash={'0' * 64} -->\n"
)


def test_extract_sections_rejects_duplicate_name() -> None:
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        extract_sections(_TWO_SHARED_A)


def test_extract_sections_rejects_duplicate_name_allow_legacy() -> None:
    # Legacy tolerance covers missing-keyword / missing-hash, NOT duplicate
    # names — a duplicate is structural corruption, not a pre-hash artifact.
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        extract_sections(_TWO_SHARED_A, allow_legacy=True)


def test_section_semantics_rejects_duplicate_name() -> None:
    with pytest.raises(MarkerError, match=r"duplicate user-section name 'A'"):
        section_semantics(_TWO_SHARED_A)


def test_repeated_unnamed_sections_do_not_collide() -> None:
    # Unnamed sections are keyed positionally ("0", "1", ...) so two of them
    # are NOT duplicates — the guard keys on the section key, not the text.
    text = (
        "<!-- setforge:user-section start shared -->\n"
        "B0\n"
        f"<!-- setforge:user-section end shared hash={'0' * 64} -->\n"
        "<!-- setforge:user-section start shared -->\n"
        "B1\n"
        f"<!-- setforge:user-section end shared hash={'0' * 64} -->\n"
    )
    assert extract_sections(text) == {"0": "B0\n", "1": "B1\n"}


# ---------------------------------------------------------------------------
# allow_legacy migration mode + the on-disk pre-hash fixture
# ---------------------------------------------------------------------------


_LEGACY_UNTAGGED_TEXT: str = (
    "<!-- setforge:user-section start workflow -->\n"
    "rule 1\nrule 2\n"
    "<!-- setforge:user-section end workflow -->\n"
)
_LEGACY_TAGGED_HASHLESS_TEXT: str = (
    "<!-- setforge:user-section start shared workflow -->\n"
    "rule 1\nrule 2\n"
    "<!-- setforge:user-section end shared workflow -->\n"
)


def test_allow_legacy_yields_shared_semantics() -> None:
    assert extract_sections(_LEGACY_UNTAGGED_TEXT, allow_legacy=True) == {
        "workflow": "rule 1\nrule 2\n"
    }
    assert section_semantics(_LEGACY_UNTAGGED_TEXT, allow_legacy=True) == {
        "workflow": SectionSemantics.SHARED
    }


def test_strict_default_rejects_missing_semantics() -> None:
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(_LEGACY_UNTAGGED_TEXT)


def test_strict_default_rejects_missing_hash() -> None:
    with pytest.raises(MarkerError, match="missing required 'hash="):
        extract_sections(_LEGACY_TAGGED_HASHLESS_TEXT)


def test_allow_legacy_tolerates_missing_hash() -> None:
    assert extract_sections(_LEGACY_TAGGED_HASHLESS_TEXT, allow_legacy=True) == {
        "workflow": "rule 1\nrule 2\n",
    }


def test_walk_markers_strict_rejects_pre_hash_fixture() -> None:
    fixture_text = (
        Path(__file__).parent / "fixtures" / "pre_hash_CLAUDE.md"
    ).read_text(encoding="utf-8")
    with pytest.raises(MarkerError, match="missing required"):
        extract_sections(fixture_text)


def test_walk_markers_allow_legacy_accepts_pre_hash_fixture() -> None:
    fixture_text = (
        Path(__file__).parent / "fixtures" / "pre_hash_CLAUDE.md"
    ).read_text(encoding="utf-8")
    bodies = extract_sections(fixture_text, allow_legacy=True)
    assert set(bodies) == {"workflow", "commits"}
    assert "Stay focused" in bodies["workflow"]
    assert "imperative mood" in bodies["commits"]


# ---------------------------------------------------------------------------
# _walk_markers event stream + the semantics-comparison invariant
# ---------------------------------------------------------------------------


def test_walk_markers_yields_end_marker_event() -> None:
    doc = (
        "before\n"
        "<!-- setforge:user-section start host-local NAME -->\n"
        "live body\n"
        f"<!-- setforge:user-section end host-local NAME hash={'a' * 64} -->\n"
        "after\n"
    )
    ends = [e for e in _walk_markers(doc) if isinstance(e, _EndMarker)]
    assert len(ends) == 1
    assert ends[0].semantics is SectionSemantics.HOST_LOCAL
    assert ends[0].key == "NAME"


def test_semantics_comparison_uses_value_equality() -> None:
    """The end-marker semantics guard compares with ``!=``, not ``is``/``is not``.

    Parses ``_handle_end_marker`` and confirms exactly one ``semantics``
    comparison against ``state.section_semantics``, and that it is a ``NotEq``
    (``!=``) comparison rather than an identity (``is``/``is not``) check —
    the style rule reserving ``is`` for None/True/False/sentinels.
    """
    source = inspect.getsource(frozen._handle_end_marker)
    tree = ast.parse(textwrap.dedent(source))
    matches: list[ast.Compare] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        right = node.comparators[0] if node.comparators else None
        if (
            isinstance(left, ast.Name)
            and left.id == "semantics"
            and isinstance(right, ast.Attribute)
            and right.attr == "section_semantics"
        ):
            matches.append(node)
    assert len(matches) == 1, "expected exactly one semantics comparison"
    (op,) = matches[0].ops
    assert isinstance(op, ast.NotEq), "semantics comparison must use != (NotEq)"
