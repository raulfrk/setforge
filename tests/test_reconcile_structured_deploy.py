"""Stage-B: the structured (key-aware) deploy entry ``reconcile_structured_file``.

The plain 3-way engine deploys line files; structured (yaml/json/jsonc) files need
a key-aware merge so an independent-key upstream change does not false-conflict with
a host edit (the case a line merge fails on). ``reconcile_structured_file`` uses
``merge_structural`` as a clean-fast-path and falls back to the proven line path
(``reconcile_plain_file``: wizard / --auto / DEFERRED) for a genuine same-key
collision — so no new conflict wizard is introduced.

Pins: independent-key host+upstream edits merge CLEAN (the subsumption win); a
same-key collision non-interactively DEFERS (keep live, base not advanced); an
unchanged upstream is a NOOP; a base-absent divergent live seeds keep-live.
"""

from __future__ import annotations

import pytest

from setforge.locking import profile_lock
from setforge.reconcile import file_id, read_base, record
from setforge.reconcile.structured_units import StructuredFormat
from setforge.reconcile_apply import AutoSide, ReconcileKind, reconcile_structured_file

_P = "default"
_FMT = StructuredFormat.YAML


def _seed(fid, *, base: bytes, local: bytes) -> None:
    with profile_lock(_P):
        record(_P, fid, base=base, local=local)


def test_independent_key_edits_merge_clean() -> None:
    """Host edits one key, upstream adds another -> key-aware merge is CLEAN
    (a line merge false-conflicts here)."""
    fid = file_id("conf")
    base = b"editor:\n  fontSize: 12\n"
    host = b"editor:\n  fontSize: 18\n"  # host bumped size
    _seed(fid, base=base, local=host)

    upstream = b"editor:\n  fontSize: 12\n  theme: dark\n"  # upstream added a key
    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert isinstance(out.content, bytes)
    assert b"fontSize: 18" in out.content  # host edit preserved
    assert b"theme: dark" in out.content  # upstream key flowed in
    assert out.new_base == upstream


def test_same_key_collision_defers_non_interactively() -> None:
    """Both sides change the SAME key -> a genuine conflict -> DEFERRED via the
    line fallback (keep live, base not advanced)."""
    fid = file_id("conf")
    base = b"key: 1\n"
    host = b"key: 99\n"
    _seed(fid, base=base, local=host)

    upstream = b"key: 2\n"
    out = reconcile_structured_file(
        _P, fid, live=host, tracked=upstream, fmt=_FMT, interactive=False
    )

    assert out.kind is ReconcileKind.DEFERRED
    assert read_base(_P, fid) == base  # base NOT advanced


def test_no_upstream_change_is_noop() -> None:
    """base == tracked and live already reconciled -> NOOP (no store churn)."""
    fid = file_id("conf")
    doc = b"a: 1\nb: 2\n"
    _seed(fid, base=doc, local=doc)

    out = reconcile_structured_file(_P, fid, live=doc, tracked=doc, fmt=_FMT)

    assert out.kind is ReconcileKind.NOOP


def test_base_absent_divergent_live_seeds_keep_live() -> None:
    """No recorded base + divergent live, non-interactive -> seed keep-live."""
    fid = file_id("fresh")
    host = b"x: 1\n"
    tracked = b"x: 2\n"
    # no _seed(): base is absent
    out = reconcile_structured_file(_P, fid, live=host, tracked=tracked, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == host  # kept live
    assert out.new_base == tracked  # base recorded as upstream
    assert out.seeded is True


def test_divergent_root_shapes_defer_through_plain_fallback() -> None:
    fid = file_id("root-shape")
    base = b"key: base\n"
    host = b"- local\n"
    upstream = b"key: upstream\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.DEFERRED
    assert read_base(_P, fid) == base


@pytest.mark.parametrize(
    ("side", "expected"),
    [(AutoSide.OURS, b"- local\n"), (AutoSide.THEIRS, b"key: upstream\n")],
)
def test_divergent_root_shapes_auto_select_exact_raw_bytes(
    side: AutoSide, expected: bytes
) -> None:
    fid = file_id(f"root-shape-{side}")
    base = b"key: base\n"
    host = b"- local\n"
    upstream = b"key: upstream\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(
        _P, fid, live=host, tracked=upstream, fmt=_FMT, auto=side
    )

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream


def test_unchanged_yaml_binary_is_byte_identical_noop() -> None:
    fid = file_id("binary")
    doc = b"payload: !!binary |\n  AP8=\n"
    _seed(fid, base=doc, local=doc)

    out = reconcile_structured_file(_P, fid, live=doc, tracked=doc, fmt=_FMT)

    assert out.kind is ReconcileKind.NOOP
    assert out.content is None
    assert read_base(_P, fid) == doc


@pytest.mark.parametrize(
    ("base", "upstream"),
    [(b"base\n", b"upstream\n"), (b"- base\n", b"- upstream\n")],
)
def test_mapping_live_with_non_mapping_other_roots_defers(
    base: bytes, upstream: bytes
) -> None:
    fid = file_id(f"inverse-root-{len(base)}")
    host = b"local: keep\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.DEFERRED
    assert read_base(_P, fid) == base


@pytest.mark.parametrize(
    ("base", "upstream"),
    [(b"base\n", b"upstream\n"), (b"- base\n", b"- upstream\n")],
)
@pytest.mark.parametrize("side", [AutoSide.OURS, AutoSide.THEIRS])
def test_mapping_live_with_non_mapping_other_roots_auto_selects_raw_bytes(
    base: bytes, upstream: bytes, side: AutoSide
) -> None:
    fid = file_id(f"inverse-root-{len(base)}-{side}")
    host = b"local: keep\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(
        _P, fid, live=host, tracked=upstream, fmt=_FMT, auto=side
    )

    assert out.kind is ReconcileKind.WRITE
    assert out.content == (host if side is AutoSide.OURS else upstream)
    assert out.new_base == upstream


_JSON = StructuredFormat.JSONC

_NOTHING_TO_MERGE_TRACKED_SIDE = [
    pytest.param(
        _FMT,
        b"top: 1\nl:\n  - a   # c\n  - b\nz: 2\ny: null\n",
        b"top: 1\nl:\n  - a   # c\n  - b\nz: 9\ny: null\n",
        id="yaml-indented-sequence",
    ),
    pytest.param(
        _JSON,
        b'{\n  // old comment\n  "a": 1,\n  "b": 2\n}\n',
        b'{\n  // new comment\n  "a": 1,\n  "b": 2\n}\n',
        id="json-comment-only",
    ),
    pytest.param(
        _JSON,
        b'{\n  "a": 1,\n  "b": 2\n}\n',
        b'{\n    "a": 1,\n    "b": 2\n}\n',
        id="json-reindent",
    ),
    pytest.param(
        _JSON,
        b'{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n',
        b'{\n  "z": 0,\n  "a": 1,\n  "b": 2,\n  "c": 3,\n  "d": 4\n}\n',
        id="json-keys-added",
    ),
    pytest.param(
        _JSON,
        b'{\n  "a": 1,\n  "b": 2,\n  "c": 3\n}\n',
        b'{\n  "b": 2,\n  "c": 3\n}\n',
        id="json-key-deleted",
    ),
]


@pytest.mark.parametrize(("fmt", "base", "upstream"), _NOTHING_TO_MERGE_TRACKED_SIDE)
def test_untouched_live_takes_tracked_bytes_verbatim(
    fmt: StructuredFormat, base: bytes, upstream: bytes
) -> None:
    fid = file_id("untouched-live")
    _seed(fid, base=base, local=base)

    out = reconcile_structured_file(_P, fid, live=base, tracked=upstream, fmt=fmt)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == upstream
    assert out.new_base == upstream


@pytest.mark.parametrize(
    ("fmt", "base", "host"),
    [
        pytest.param(
            _FMT,
            b"# c\nshared: 1\nl:\n  - a\n  - b\nsib: 1\n",
            b"# c\nshared: 2  # mine\nl:\n  - a\n  - b\nsib: 1\n",
            id="yaml",
        ),
        pytest.param(
            _JSON,
            b'{\n  "a": 1 // one\n}\n',
            b'{\n  "a": 1, // one\n  "host": true\n}\n',
            id="json",
        ),
    ],
)
def test_unchanged_tracked_leaves_edited_live_alone(
    fmt: StructuredFormat, base: bytes, host: bytes
) -> None:
    fid = file_id("unchanged-tracked")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=base, fmt=fmt)

    assert out.kind is ReconcileKind.NOOP
    assert out.content is None


def test_unchanged_yaml_with_indented_sequence_is_noop() -> None:
    fid = file_id("indented")
    doc = b"# top comment\na: 1\nnested:\n  x:\n    - 1\n    - 2\n  y: null\n"
    _seed(fid, base=doc, local=doc)

    out = reconcile_structured_file(_P, fid, live=doc, tracked=doc, fmt=_FMT)

    assert out.kind is ReconcileKind.NOOP


def test_live_already_equal_to_new_tracked_only_advances_base() -> None:
    fid = file_id("converged")
    base = b"l:\n  - a\nz: 2\n"
    both = b"l:\n  - a\nz: 9\n"
    _seed(fid, base=base, local=both)

    out = reconcile_structured_file(_P, fid, live=both, tracked=both, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == both
    assert out.new_base == both


_LINE_MERGED_UNPARSEABLE = [
    pytest.param(
        b"---\nkind: A\ndata: 1\n---\nkind: B\ndata: 2\n",
        b"---\nkind: A\ndata: 7\n---\nkind: B\ndata: 2\n",
        b"---\nkind: A\ndata: 1\n---\nkind: B\ndata: 3\n",
        b"---\nkind: A\ndata: 7\n---\nkind: B\ndata: 3\n",
        id="multi-document",
    ),
    pytest.param(
        b"name: {{ foo }}\nmid: 0\nport: 1\n",
        b"name: {{ bar }}\nmid: 0\nport: 1\n",
        b"name: {{ foo }}\nmid: 0\nport: 2\n",
        b"name: {{ bar }}\nmid: 0\nport: 2\n",
        id="templated",
    ),
    pytest.param(
        b"a: 1\na: 2\nmid: 0\nport: 1\n",
        b"a: 1\na: 5\nmid: 0\nport: 1\n",
        b"a: 1\na: 2\nmid: 0\nport: 2\n",
        b"a: 1\na: 5\nmid: 0\nport: 2\n",
        id="duplicate-keys",
    ),
    pytest.param(
        b"1: a\nmid: 0\n2: b\n",
        b"1: x\nmid: 0\n2: b\n",
        b"1: a\nmid: 0\n2: y\n",
        b"1: x\nmid: 0\n2: y\n",
        id="integer-keys",
    ),
]


@pytest.mark.parametrize(
    ("base", "host", "upstream", "expected"), _LINE_MERGED_UNPARSEABLE
)
def test_yaml_the_key_engine_cannot_model_is_line_merged(
    base: bytes, host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("unmodelled")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream


_JSON_BASE = b'{\n  "a": 1\n}\n'
_JSON_UPSTREAM = b'{\n  "a": 2\n}\n'
_BROKEN_JSON = [
    pytest.param(b"", id="empty"),
    pytest.param(b'{\n  "a": 1,\n  "L', id="truncated"),
]


@pytest.mark.parametrize("broken", _BROKEN_JSON)
def test_unparseable_live_json_defers_instead_of_raising(broken: bytes) -> None:
    fid = file_id("broken-live")
    _seed(fid, base=_JSON_BASE, local=broken)

    out = reconcile_structured_file(
        _P, fid, live=broken, tracked=_JSON_UPSTREAM, fmt=_JSON
    )

    assert out.kind is ReconcileKind.DEFERRED
    assert read_base(_P, fid) == _JSON_BASE


@pytest.mark.parametrize("broken", _BROKEN_JSON)
@pytest.mark.parametrize("upstream", [_JSON_BASE, _JSON_UPSTREAM])
def test_use_tracked_replaces_unparseable_live_json(
    broken: bytes, upstream: bytes
) -> None:
    fid = file_id("broken-live-auto")
    _seed(fid, base=_JSON_BASE, local=broken)

    out = reconcile_structured_file(
        _P, fid, live=broken, tracked=upstream, fmt=_JSON, auto=AutoSide.THEIRS
    )

    assert out.kind is ReconcileKind.WRITE
    assert out.content == upstream
    assert out.new_base == upstream


@pytest.mark.parametrize("auto", [None, AutoSide.OURS])
def test_unparseable_live_json_is_kept_without_use_tracked(
    auto: AutoSide | None,
) -> None:
    fid = file_id("broken-live-kept")
    broken = b'{\n  "a": 1,\n  "L'
    _seed(fid, base=_JSON_BASE, local=broken)

    out = reconcile_structured_file(
        _P, fid, live=broken, tracked=_JSON_BASE, fmt=_JSON, auto=auto
    )

    assert out.kind is ReconcileKind.NOOP


def test_use_tracked_keeps_host_edit_when_neither_side_parses() -> None:
    fid = file_id("multidoc-host-edit")
    base = b"---\nkind: A\n---\nkind: B\n"
    host = b"---\nkind: A\n---\nkind: MINE\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(
        _P, fid, live=host, tracked=base, fmt=_FMT, auto=AutoSide.THEIRS
    )

    assert out.kind is ReconcileKind.NOOP


def test_duplicate_key_live_json_is_line_merged() -> None:
    fid = file_id("dup-live")
    base = b'{\n  "a": 1,\n  "m": 0,\n  "z": 1\n}\n'
    host = b'{\n  "a": 1,\n  "a": 5,\n  "m": 0,\n  "z": 1\n}\n'
    upstream = b'{\n  "a": 1,\n  "m": 0,\n  "z": 2\n}\n'
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == b'{\n  "a": 1,\n  "a": 5,\n  "m": 0,\n  "z": 2\n}\n'


_BYTE_PRESERVING_MERGES = [
    pytest.param(
        _FMT,
        b"top: 1\nl:\n  - a   # c\n  - b\nmid: 0\nz: 2\n",
        b"top: 5\nl:\n  - a   # c\n  - b\nmid: 0\nz: 2\n",
        b"top: 1\nl:\n  - a   # c\n  - b\nmid: 0\nz: 9\n",
        b"top: 5\nl:\n  - a   # c\n  - b\nmid: 0\nz: 9\n",
        id="yaml-indented-sequence",
    ),
    pytest.param(
        _FMT,
        b"top: 1\na: ~\nb: True\nc: +5\nd: .NaN\n? k\n: v\ne: [ a ,b ]\nz: 2\n",
        b"top: 5\na: ~\nb: True\nc: +5\nd: .NaN\n? k\n: v\ne: [ a ,b ]\nz: 2\n",
        b"top: 1\na: ~\nb: True\nc: +5\nd: .NaN\n? k\n: v\ne: [ a ,b ]\nz: 9\n",
        b"top: 5\na: ~\nb: True\nc: +5\nd: .NaN\n? k\n: v\ne: [ a ,b ]\nz: 9\n",
        id="yaml-scalar-spellings",
    ),
    pytest.param(
        _FMT,
        b"# old note\na: 1\nmid:\n    deep: [ 1,2 ]\nz: 2\n",
        b"# old note\na: 1\nmid:\n    deep: [ 1,2 ]\nz: 3   # mine\n",
        b"# new note\na: 9\nmid:\n    deep: [ 1,2 ]\nz: 2\n",
        b"# new note\na: 9\nmid:\n    deep: [ 1,2 ]\nz: 3   # mine\n",
        id="yaml-tracked-comment-and-value",
    ),
    pytest.param(
        _FMT,
        b"host: base\nshared: 1   # sh\nl:\n    - a    # first\nm: {x: 1,   y: 2}\n",
        b"host: mine\nshared: 2   # sh\nl:\n    - a    # first\nm: {x: 1,   y: 2}\n",
        b"host: base\nshared: 2   # sh\nl:\n    - a    # first\nm: {x: 1,   y: 2}\n",
        b"host: mine\nshared: 2   # sh\nl:\n    - a    # first\nm: {x: 1,   y: 2}\n",
        id="yaml-after-one-key-was-shared",
    ),
    pytest.param(
        _JSON,
        b'{\n  // old comment\n  "a": 1,\n  "m": 0,\n  "b": 2\n}\n',
        b'{\n  // old comment\n  "a": 1,\n  "m": 0,\n  "b": 3\n}\n',
        b'{\n  // NEW comment\n  "a": 9,\n  "m": 0,\n  "b": 2\n}\n',
        b'{\n  // NEW comment\n  "a": 9,\n  "m": 0,\n  "b": 3\n}\n',
        id="json-tracked-comment-and-value",
    ),
]


@pytest.mark.parametrize(
    ("fmt", "base", "host", "upstream", "expected"), _BYTE_PRESERVING_MERGES
)
def test_independent_edits_keep_untouched_lines_byte_identical(
    fmt: StructuredFormat, base: bytes, host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("line-preserving")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=fmt)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream


@pytest.mark.parametrize(
    ("fmt", "base", "host", "upstream", "expected"),
    [
        pytest.param(
            _FMT,
            b"a: 1\nm: 0\nz: 2\n",
            b"a: 1\nnew: 1\nm: 0\nz: 2\n",
            b"a: 1\nm: 0\nz: 2\nnew: 1\n",
            b"a: 1\nnew: 1\nm: 0\nz: 2\n",
            id="yaml",
        ),
        pytest.param(
            _JSON,
            b'{\n  "a": 1,\n  "m": 0,\n  "y": 0,\n  "z": 2\n}\n',
            b'{\n  "a": 1,\n  "new": 1,\n  "m": 0,\n  "y": 0,\n  "z": 2\n}\n',
            b'{\n  "a": 1,\n  "m": 0,\n  "y": 0,\n  "z": 2,\n  "new": 1\n}\n',
            b'{\n  "a": 1,\n  "new": 1,\n  "m": 0,\n  "y": 0,\n  "z": 2\n}\n',
            id="json",
        ),
    ],
)
def test_line_merge_that_would_duplicate_a_key_yields_to_the_key_merge(
    fmt: StructuredFormat, base: bytes, host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("same-key-both")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=fmt)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream


def test_adjacent_line_edits_merge_by_key_with_exact_bytes() -> None:
    fid = file_id("adjacent")
    base = b"editor:\n  fontSize: 12\n"
    host = b"editor:\n  fontSize: 18\n"
    upstream = b"editor:\n  fontSize: 12\n  theme: dark\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == b"editor:\n  fontSize: 18\n  theme: dark\n"


@pytest.mark.parametrize(
    ("fmt", "base", "host", "upstream", "expected"),
    [
        pytest.param(
            _FMT,
            b"editor:\n    fontSize: 12   # px\nl:\n    - a\nt: ~\n",
            b"editor:\n    fontSize: 18   # px\nl:\n    - a\nt: ~\n",
            b"editor:\n    fontSize: 12   # px\n    theme: dark\nl:\n    - a\nt: ~\n",
            b"editor:\n    fontSize: 18   # px\n    theme: dark\nl:\n    - a\nt: ~\n",
            id="yaml-upstream-adds-next-to-host-edit",
        ),
        pytest.param(
            _FMT,
            b"a: 1\nb: 2\nd: 0\nl:\n    - x\n",
            b"a: 5\nb: 2\nd: 0\nl:\n    - x\n",
            b"a: 1\nd: 0\nl:\n    - x\nc: 4\n",
            b"a: 5\nd: 0\nl:\n    - x\nc: 4\n",
            id="yaml-upstream-deletes-next-to-host-edit",
        ),
        pytest.param(
            _JSON,
            b'{\n    "a": 1,\n    "b": 2,\n}\n',
            b'{\n    "a": 5,\n    "b": 2,\n}\n',
            b'{\n    "a": 1,\n    "b": 3,\n    "c": 4,\n}\n',
            b'{\n    "a": 5,\n    "b": 3,\n    "c": 4,\n}\n',
            id="json-trailing-commas",
        ),
    ],
)
def test_adjacent_edits_keep_the_live_layout(
    fmt: StructuredFormat, base: bytes, host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("adjacent-layout")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=fmt)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream


def test_two_keys_merged_on_one_line_are_re_serialised() -> None:
    fid = file_id("one-line")
    base = b"m: {x: 1, y: 2}\nz: 0\n"
    host = b"m: {x: 5, y: 2}\nz: 0\n"
    upstream = b"m: {x: 1, y: 7}\nz: 0\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == b"m: {x: 5, y: 7}\nz: 0\n"
