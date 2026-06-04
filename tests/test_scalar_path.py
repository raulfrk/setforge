"""Tests for setforge.scalar_path — read-at-path + single-scalar splice.

Covers the read seam (ABSENT vs present-null distinction on both YAML and
JSONC), the write seam (set / create / delete, comment-preserving, golden
strings), and the two refusal contracts (list-suffix paths, missing-parent
paths). A json-five round-trip case proves a created key survives re-dump +
re-parse with its formatting intact.
"""

import io

import pytest
from json5.dumper import ModelDumper, dumps
from json5.loader import ModelLoader, loads
from ruamel.yaml import YAML

from setforge.errors import MergeTypeMismatch
from setforge.scalar_merge import (
    ABSENT,
    ScalarOutcome,
    ScalarResolution,
)
from setforge.scalar_path import (
    read_scalar_jsonc,
    read_scalar_yaml,
    write_scalar_jsonc,
    write_scalar_yaml,
)


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.indent(mapping=2, sequence=4, offset=2)
    y.preserve_quotes = True
    return y


def _load_yaml(text: str) -> object:
    return _yaml().load(text)


def _dump_yaml(doc: object) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def _load_jsonc(text: str) -> object:
    return loads(text, loader=ModelLoader())


def _dump_jsonc(model: object) -> str:
    return dumps(model, dumper=ModelDumper())


# ---------------------------------------------------------------------------
# READ: ABSENT vs null vs typed value, on both formats.
# ---------------------------------------------------------------------------


def test_read_yaml_absent_key_returns_absent() -> None:
    doc = _load_yaml("a:\n  b: 1\n")
    assert read_scalar_yaml(doc, "a.missing") is ABSENT


def test_read_yaml_present_null_returns_none() -> None:
    doc = _load_yaml("a:\n  b: null\n")
    assert read_scalar_yaml(doc, "a.b") is None


def test_read_jsonc_absent_key_returns_absent() -> None:
    model = _load_jsonc('{\n  "a": {\n    "b": 1\n  }\n}')
    assert read_scalar_jsonc(model, "a > missing") is ABSENT


def test_read_jsonc_present_null_returns_none() -> None:
    model = _load_jsonc('{\n  "a": {\n    "b": null\n  }\n}')
    assert read_scalar_jsonc(model, "a > b") is None


def test_read_yaml_present_scalar_returns_typed_value() -> None:
    doc = _load_yaml("a:\n  i: 7\n  s: hi\n  flag: true\n")
    assert read_scalar_yaml(doc, "a.i") == 7
    assert read_scalar_yaml(doc, "a.s") == "hi"
    assert read_scalar_yaml(doc, "a.flag") is True


def test_read_jsonc_present_scalar_returns_typed_value() -> None:
    model = _load_jsonc(
        '{\n  "a": {\n    "i": 7,\n    "s": "hi",\n    "flag": true\n  }\n}'
    )
    assert read_scalar_jsonc(model, "a > i") == 7
    assert read_scalar_jsonc(model, "a > s") == "hi"
    assert read_scalar_jsonc(model, "a > flag") is True


def test_read_yaml_missing_intermediate_returns_absent() -> None:
    """A missing PARENT on a read path surfaces as ABSENT, not an error."""
    doc = _load_yaml("a: 1\n")
    assert read_scalar_yaml(doc, "x.y.z") is ABSENT


def test_read_jsonc_missing_intermediate_returns_absent() -> None:
    model = _load_jsonc('{\n  "a": 1\n}')
    assert read_scalar_jsonc(model, "x > y > z") is ABSENT


# ---------------------------------------------------------------------------
# WRITE (YAML): set / create / delete, comment-preserving golden strings.
# ---------------------------------------------------------------------------

_YAML_FIXTURE = """# header comment
a: 1  # inline on a
b:
  c: 2  # inline on c
  d: 3  # inline on d
"""


def test_write_yaml_set_overwrites_existing_preserving_siblings() -> None:
    doc = _load_yaml(_YAML_FIXTURE)
    write_scalar_yaml(doc, "b.c", ScalarResolution(ScalarOutcome.TAKE, 99))
    expected = """# header comment
a: 1  # inline on a
b:
  c: 99 # inline on c
  d: 3  # inline on d
"""
    assert _dump_yaml(doc) == expected


def test_write_yaml_create_appends_leaf_in_existing_parent() -> None:
    doc = _load_yaml(_YAML_FIXTURE)
    write_scalar_yaml(doc, "b.e", ScalarResolution(ScalarOutcome.TAKE, 5))
    expected = """# header comment
a: 1  # inline on a
b:
  c: 2  # inline on c
  d: 3  # inline on d
  e: 5
"""
    assert _dump_yaml(doc) == expected


def test_write_yaml_delete_removes_leaf_preserving_siblings() -> None:
    doc = _load_yaml(_YAML_FIXTURE)
    write_scalar_yaml(doc, "b.d", ScalarResolution(ScalarOutcome.DELETE))
    expected = """# header comment
a: 1  # inline on a
b:
  c: 2  # inline on c
"""
    assert _dump_yaml(doc) == expected


def test_write_yaml_create_top_level_leaf() -> None:
    doc = _load_yaml("a: 1  # keep\n")
    write_scalar_yaml(doc, "z", ScalarResolution(ScalarOutcome.TAKE, "new"))
    assert _dump_yaml(doc) == "a: 1  # keep\nz: new\n"


# ---------------------------------------------------------------------------
# WRITE (JSONC): set / create / delete, comment-preserving golden strings.
# Fixtures carry a trailing comma after the last key so an appended leaf
# lands cleanly (json-five's dumper otherwise emits a dangling comma line).
# ---------------------------------------------------------------------------

_JSONC_FIXTURE = """{
  // header
  "a": {
    "c": 2,  // inline on c
    "d": 3,  // inline on d
  },
}"""


def test_write_jsonc_set_overwrites_existing_preserving_siblings() -> None:
    model = _load_jsonc(_JSONC_FIXTURE)
    write_scalar_jsonc(model, "a > c", ScalarResolution(ScalarOutcome.TAKE, 99))
    expected = """{
  // header
  "a": {
    "c": 99,  // inline on c
    "d": 3,  // inline on d
  },
}"""
    assert _dump_jsonc(model) == expected


def test_write_jsonc_create_appends_leaf_in_existing_parent() -> None:
    model = _load_jsonc(_JSONC_FIXTURE)
    write_scalar_jsonc(model, "a > e", ScalarResolution(ScalarOutcome.TAKE, 5))
    expected = """{
  // header
  "a": {
    "c": 2,  // inline on c
    "d": 3,  // inline on d
    "e": 5,
  },
}"""
    assert _dump_jsonc(model) == expected


def test_write_jsonc_delete_removes_leaf_preserving_siblings() -> None:
    model = _load_jsonc(_JSONC_FIXTURE)
    write_scalar_jsonc(model, "a > d", ScalarResolution(ScalarOutcome.DELETE))
    expected = """{
  // header
  "a": {
    "c": 2,  // inline on c
  },
}"""
    assert _dump_jsonc(model) == expected


def test_write_jsonc_created_key_survives_roundtrip() -> None:
    """A spliced KeyValuePair must re-dump + re-parse with its formatting.

    Guards the json-five caveat: ``key_value_pairs`` is a derived property,
    so a created leaf must be appended to ``.keys`` / ``.values`` (the stored
    fields) to survive a dump -> parse cycle.
    """
    model = _load_jsonc(_JSONC_FIXTURE)
    write_scalar_jsonc(model, "a > e", ScalarResolution(ScalarOutcome.TAKE, 5))
    redumped = _dump_jsonc(model)
    reparsed = loads(redumped)
    assert reparsed["a"]["e"] == 5
    # The created key reads back through our own reader too.
    remodel = _load_jsonc(redumped)
    assert read_scalar_jsonc(remodel, "a > e") == 5


def test_write_jsonc_null_resolution_sets_null_literal() -> None:
    model = _load_jsonc(_JSONC_FIXTURE)
    write_scalar_jsonc(model, "a > c", ScalarResolution(ScalarOutcome.TAKE, None))
    assert read_scalar_jsonc(_load_jsonc(_dump_jsonc(model)), "a > c") is None


# ---------------------------------------------------------------------------
# REFUSALS: list-suffix paths, missing-parent paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["a[*]", "a.b[]", "a.b.c[*]"])
def test_read_yaml_rejects_list_suffix(path: str) -> None:
    doc = _load_yaml("a:\n  b: 1\n")
    with pytest.raises(ValueError, match="list suffix not allowed"):
        read_scalar_yaml(doc, path)


@pytest.mark.parametrize("path", ["a[*]", "a.b[]"])
def test_write_yaml_rejects_list_suffix(path: str) -> None:
    doc = _load_yaml("a:\n  b: 1\n")
    with pytest.raises(ValueError, match="list suffix not allowed"):
        write_scalar_yaml(doc, path, ScalarResolution(ScalarOutcome.TAKE, 1))


@pytest.mark.parametrize("path", ["a[*]", "a > b[]"])
def test_read_jsonc_rejects_list_suffix(path: str) -> None:
    model = _load_jsonc('{\n  "a": {\n    "b": 1\n  }\n}')
    with pytest.raises(ValueError, match="list suffix not allowed"):
        read_scalar_jsonc(model, path)


@pytest.mark.parametrize("path", ["a[*]", "a > b[]"])
def test_write_jsonc_rejects_list_suffix(path: str) -> None:
    model = _load_jsonc('{\n  "a": {\n    "b": 1\n  }\n}')
    with pytest.raises(ValueError, match="list suffix not allowed"):
        write_scalar_jsonc(model, path, ScalarResolution(ScalarOutcome.TAKE, 1))


def test_write_yaml_missing_parent_raises_no_vivify() -> None:
    doc = _load_yaml("a: 1\n")
    with pytest.raises(KeyError, match=r"missing parent"):
        write_scalar_yaml(doc, "a.b.c", ScalarResolution(ScalarOutcome.TAKE, 1))
    # No structure was created.
    assert _dump_yaml(doc) == "a: 1\n"


def test_write_jsonc_missing_parent_raises_no_vivify() -> None:
    """A genuinely-absent intermediate object raises KeyError (no vivify)."""
    model = _load_jsonc('{\n  "a": {\n    "b": 1\n  }\n}')
    with pytest.raises(KeyError, match=r"missing parent"):
        write_scalar_jsonc(model, "x > y > z", ScalarResolution(ScalarOutcome.TAKE, 1))
    assert _dump_jsonc(model) == '{\n  "a": {\n    "b": 1\n  }\n}'


def test_write_jsonc_scalar_intermediate_raises_type_mismatch() -> None:
    """An intermediate that exists but is a scalar raises MergeTypeMismatch."""
    model = _load_jsonc('{\n  "a": 1\n}')
    with pytest.raises(MergeTypeMismatch):
        write_scalar_jsonc(model, "a > b > c", ScalarResolution(ScalarOutcome.TAKE, 1))
    assert _dump_jsonc(model) == '{\n  "a": 1\n}'


# ---------------------------------------------------------------------------
# READ: non-scalar leaf rejected symmetrically on both formats.
# ---------------------------------------------------------------------------


def test_read_yaml_mapping_leaf_raises_type_mismatch() -> None:
    """A YAML path terminating on a mapping is rejected at the read seam."""
    doc = _load_yaml("a:\n  b: 1\n")
    with pytest.raises(MergeTypeMismatch, match="does not terminate on a scalar"):
        read_scalar_yaml(doc, "a")


def test_read_yaml_sequence_leaf_raises_type_mismatch() -> None:
    """A YAML path terminating on a sequence is rejected at the read seam."""
    doc = _load_yaml("a:\n  - 1\n  - 2\n")
    with pytest.raises(MergeTypeMismatch, match="does not terminate on a scalar"):
        read_scalar_yaml(doc, "a")


def test_read_jsonc_object_leaf_raises_type_mismatch() -> None:
    """A JSONC path terminating on an object is rejected at the read seam."""
    model = _load_jsonc('{\n  "a": {\n    "b": 1\n  }\n}')
    with pytest.raises(MergeTypeMismatch, match="does not terminate on a scalar"):
        read_scalar_jsonc(model, "a")


def test_read_jsonc_array_leaf_raises_type_mismatch() -> None:
    """A JSONC path terminating on an array is rejected at the read seam."""
    model = _load_jsonc('{\n  "a": [\n    1,\n    2\n  ]\n}')
    with pytest.raises(MergeTypeMismatch, match="does not terminate on a scalar"):
        read_scalar_jsonc(model, "a")


def test_write_yaml_delete_missing_leaf_is_noop() -> None:
    """DELETE of an already-absent leaf in an existing parent is a no-op."""
    doc = _load_yaml("a:\n  b: 1\n")
    write_scalar_yaml(doc, "a.gone", ScalarResolution(ScalarOutcome.DELETE))
    assert _dump_yaml(doc) == "a:\n  b: 1\n"


def test_write_jsonc_delete_missing_leaf_is_noop() -> None:
    model = _load_jsonc('{\n  "a": {\n    "b": 1\n  }\n}')
    write_scalar_jsonc(model, "a > gone", ScalarResolution(ScalarOutcome.DELETE))
    assert _dump_jsonc(model) == '{\n  "a": {\n    "b": 1\n  }\n}'
