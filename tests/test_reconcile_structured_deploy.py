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


_ANCHORED = (
    b"top: 1\ndefaults: &d\n  retries: 3\n  tags: [a, b]\n"
    b"svc1:\n  <<: *d\n  name: one\nz: 1\n"
)


@pytest.mark.parametrize(
    ("host", "upstream", "expected"),
    [
        pytest.param(
            _ANCHORED.replace(b"top: 1", b"top: 5"),
            _ANCHORED.replace(b"retries: 3", b"retries: 9"),
            _ANCHORED.replace(b"top: 1", b"top: 5").replace(
                b"retries: 3", b"retries: 9"
            ),
            id="upstream-changes-anchored-value",
        ),
        pytest.param(
            _ANCHORED.replace(b"top: 1", b"top: 5"),
            _ANCHORED.replace(b"z: 1", b"z: 2"),
            _ANCHORED.replace(b"top: 1", b"top: 5").replace(b"z: 1", b"z: 2"),
            id="unrelated-keys",
        ),
        pytest.param(
            _ANCHORED.replace(b"top: 1", b"top: 5"),
            _ANCHORED + b"svc3:\n  <<: *d\n  name: three\n",
            _ANCHORED.replace(b"top: 1", b"top: 5")
            + b"svc3:\n  <<: *d\n  name: three\n",
            id="upstream-adds-merge-key-user",
        ),
    ],
)
def test_anchors_and_merge_keys_survive_a_merge(
    host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("anchors")
    _seed(fid, base=_ANCHORED, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream


_ALIAS_ONE_LINE = [
    pytest.param(
        b"defaults: &d\n  tags: [a, b]\nsvc1: {<<: *d, name: one, port: 1}\n",
        id="merge-key",
    ),
    pytest.param(
        b"tags: &t [a, b]\nsvc1: {tags: *t, name: one, port: 1}\n",
        id="alias",
    ),
]


@pytest.mark.parametrize("base", _ALIAS_ONE_LINE)
def test_alias_bearing_yaml_is_never_re_serialised(base: bytes) -> None:
    fid = file_id("anchors-one-line")
    host = base.replace(b"name: one", b"name: uno")
    upstream = base.replace(b"port: 1", b"port: 2")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.DEFERRED
    assert read_base(_P, fid) == base


@pytest.mark.parametrize("base", _ALIAS_ONE_LINE)
@pytest.mark.parametrize("side", [AutoSide.OURS, AutoSide.THEIRS])
def test_alias_bearing_yaml_conflict_auto_takes_exact_side(
    base: bytes, side: AutoSide
) -> None:
    fid = file_id(f"anchors-one-line-{side}")
    host = base.replace(b"name: one", b"name: uno")
    upstream = base.replace(b"port: 1", b"port: 2")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(
        _P, fid, live=host, tracked=upstream, fmt=_FMT, auto=side
    )

    assert out.kind is ReconcileKind.WRITE
    assert out.content == (host if side is AutoSide.OURS else upstream)


def test_merged_strict_json_stays_strict_json() -> None:
    import json

    fid = file_id("strict-json")
    base = b'{\n  "a": 1,\n  "b": 2\n}\n'
    host = b'{\n  "a": 5,\n  "b": 2\n}\n'
    upstream = b'{\n  "a": 1\n}\n'
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.WRITE
    assert isinstance(out.content, bytes)
    assert json.loads(out.content) == {"a": 5}


def test_json5_syntax_already_in_use_may_stay_in_the_merge() -> None:
    fid = file_id("json5-in-use")
    base = b'{\n  "a": 1,\n  "b": 2,\n}\n'
    host = b'{\n  "a": 5,\n  "b": 2,\n}\n'
    upstream = b'{\n  "a": 1,\n}\n'
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == b'{\n  "a": 5,\n}\n'


@pytest.mark.parametrize(
    ("base", "host", "upstream", "expected"),
    [
        pytest.param(
            b'{\n  "a": 1,\n  "c": 3 // last\n}\n',
            b'{\n  "a": 1,\n  "c": 3, // last\n  "hostonly": true\n}\n',
            b'{\n  "a": 1,\n  "c": 4 // last\n}\n',
            b'{\n  "a": 1,\n  "c": 4, // last\n  "hostonly": true\n}\n',
            id="host-appends-upstream-edits-last-key",
        ),
        pytest.param(
            b'{\n  "editor.fontSize": 12,\n  "files.autoSave": "off"\n}\n',
            b'{\n  "editor.fontSize": 12,\n  "files.autoSave": "off",\n'
            b'  "claudeCode.local": true\n}\n',
            b'{\n  "editor.fontSize": 12,\n  "files.autoSave": "off",\n'
            b'  "editor.tabSize": 2\n}\n',
            b'{\n  "editor.fontSize": 12,\n  "files.autoSave": "off",\n'
            b'  "claudeCode.local": true,\n  "editor.tabSize": 2\n}\n',
            id="both-append-a-key",
        ),
        pytest.param(
            b'{\n  "a": 1,\n  "b": 2\n}\n',
            b'{\n  "a": 5,\n  "b": 2\n}\n',
            b'{\n  "a": 1\n}\n',
            b'{\n  "a": 5\n}\n',
            id="upstream-deletes-last-key",
        ),
    ],
)
def test_json_key_merge_follows_the_live_layout(
    base: bytes, host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("json-layout")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream


_ONE_LINE = b"m: {x: 1, y: 2}\n"
_REDUMPED_LAYOUTS = [
    pytest.param(b"\xef\xbb\xbf" + _ONE_LINE + b"z: 2\n", id="bom"),
    pytest.param(b"top: 1  # one\r\nm: {x: 1, y: 2}\r\nn:\r\n  a: 1\r\n", id="crlf"),
    pytest.param(
        b"%YAML 1.2\n---\n" + _ONE_LINE + b"z: 2\n...\n", id="directive-and-end"
    ),
    pytest.param(b"# head\n\n---\n# c\n" + _ONE_LINE + b"z: 2\n", id="document-start"),
    pytest.param(_ONE_LINE + b"z: 2", id="no-final-newline"),
    pytest.param(
        _ONE_LINE + b"l:\n  - a   # c\n  - b\nz: 2\n", id="sequence-indented-by-two"
    ),
    pytest.param(
        _ONE_LINE
        + b"l:\n    - a    # first\n    - b\n"
        + b"n:\n    x: 1   # xx\n    o:\n        - q\n",
        id="four-space-indent",
    ),
    pytest.param(
        _ONE_LINE + b"n:\n    deep: 1\nl:\n- k: 1\n  v: 2\n",
        id="flush-sequence-wide-mapping",
    ),
    pytest.param(
        _ONE_LINE + b"l:\n  - k:\n      - x\n    v: 2\n", id="nested-sequences"
    ),
    pytest.param(
        _ONE_LINE + b"s: |\n  - not a list\n  k:\n      v\nz: 2\n", id="plain"
    ),
]


@pytest.mark.parametrize("base", _REDUMPED_LAYOUTS)
def test_re_serialised_yaml_keeps_the_source_byte_layout(base: bytes) -> None:
    fid = file_id("redumped")
    host = base.replace(b"x: 1, y: 2", b"x: 5, y: 2")
    upstream = base.replace(b"x: 1, y: 2", b"x: 1, y: 7")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == base.replace(b"x: 1, y: 2", b"x: 5, y: 7")


_KEYS = b'[\n  {"key":"a","command":"x"},\n  {"key":"b","command":"y"}\n]\n'


@pytest.mark.parametrize(
    ("host", "upstream", "expected"),
    [
        pytest.param(
            b'[\n  {"key":"a","command":"x"},\n  {"key":"b","command":"y"},\n'
            b'  {"key":"L","command":"l"}\n]\n',
            b'[\n  {"key":"a","command":"x2"},\n  {"key":"b","command":"y"}\n]\n',
            b'[\n  {"key":"a","command":"x2"},\n  {"key":"b","command":"y"},\n'
            b'  {"key":"L","command":"l"}\n]\n',
            id="upstream-edits-first-host-appends",
        ),
        pytest.param(
            b'[\n  {"key":"a","command":"x"},\n  {"key":"b","command":"y"},\n'
            b'  {"key":"L","command":"l"}\n]\n',
            b'[\n  {"key":"a","command":"x"},\n  {"key":"b","command":"y2"}\n]\n',
            b'[\n  {"key":"a","command":"x"},\n  {"key":"b","command":"y2"},\n'
            b'  {"key":"L","command":"l"}\n]\n',
            id="upstream-edits-last-host-appends",
        ),
        pytest.param(
            b'[\n  {"key":"a","command":"x"},\n  {"key":"M","command":"m"},\n'
            b'  {"key":"b","command":"y"}\n]\n',
            b'[\n  {"key":"a","command":"x2"},\n  {"key":"b","command":"y"}\n]\n',
            b'[\n  {"key":"a","command":"x2"},\n  {"key":"M","command":"m"},\n'
            b'  {"key":"b","command":"y"}\n]\n',
            id="host-inserts-after-the-element-upstream-edits",
        ),
        pytest.param(
            b'[\n  {"key":"M","command":"m"},\n  {"key":"a","command":"x"},\n'
            b'  {"key":"b","command":"y"}\n]\n',
            b'[\n  {"key":"a","command":"x2"},\n  {"key":"b","command":"y"}\n]\n',
            b'[\n  {"key":"M","command":"m"},\n  {"key":"a","command":"x2"},\n'
            b'  {"key":"b","command":"y"}\n]\n',
            id="host-inserts-before-the-element-upstream-edits",
        ),
    ],
)
def test_json_array_root_merges_element_wise(
    host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("array-root")
    _seed(fid, base=_KEYS, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream


@pytest.mark.parametrize(
    ("host", "upstream"),
    [
        pytest.param(
            b'[\n  {"key":"a","command":"mine"},\n  {"key":"b","command":"y"}\n]\n',
            b'[\n  {"key":"a","command":"theirs"},\n  {"key":"b","command":"y"}\n]\n',
            id="same-element",
        ),
        pytest.param(
            b'[\n  {"key":"a","command":"x"},\n  {"key":"b","command":"y"},\n'
            b'  {"key":"L","command":"l"}\n]\n',
            b'[\n  {"key":"a","command":"x"},\n  {"key":"b","command":"y"},\n'
            b'  {"key":"T","command":"t"}\n]\n',
            id="both-append",
        ),
        pytest.param(
            b'[\n  {"key":"b","command":"y"}\n]\n',
            b'[\n  {"key":"a","command":"theirs"},\n  {"key":"b","command":"y"}\n]\n',
            id="host-deletes-the-element-upstream-edits",
        ),
        pytest.param(
            b'[\n  {"key":"a","command":"x"},\n  {"key":"b","command":"mine"}\n]\n',
            b'[\n  {"key":"a","command":"x"},\n  {"key":"b","command":"y"},\n'
            b'  {"key":"T","command":"t"}\n]\n',
            id="host-edits-last-upstream-appends",
        ),
        pytest.param(
            b'[\n  {"key":"a","command":"theirs"},\n  {"key":"b","command":"y"}\n]\n',
            b'[\n  {"key":"b","command":"y"}\n]\n',
            id="upstream-deletes-the-element-host-edits",
        ),
    ],
)
def test_json_array_root_same_position_edits_still_conflict(
    host: bytes, upstream: bytes
) -> None:
    fid = file_id("array-root-conflict")
    _seed(fid, base=_KEYS, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.DEFERRED
    assert read_base(_P, fid) == _KEYS


def test_json_array_root_insertion_inside_a_replaced_run_conflicts() -> None:
    fid = file_id("array-root-inside")
    base = b"[\n  1,\n  2,\n  3,\n  4\n]\n"
    host = b"[\n  1,\n  2,\n  9,\n  3,\n  4\n]\n"
    upstream = b"[\n  1,\n  7,\n  8,\n  6,\n  4\n]\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.DEFERRED


def test_json_array_root_insertion_between_two_edited_elements_merges() -> None:
    fid = file_id("array-root-between")
    base = b"[\n  1,\n  2,\n  3,\n  4\n]\n"
    host = b"[\n  1,\n  2,\n  9,\n  3,\n  4\n]\n"
    upstream = b"[\n  1,\n  7,\n  8,\n  4\n]\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == b"[\n  1,\n  7,\n  9,\n  8,\n  4\n]\n"


def test_json_array_root_identical_edits_on_both_sides_merge() -> None:
    fid = file_id("array-root-same")
    base = b"[\n  1,\n  2,\n  3,\n  4\n]\n"
    host = b"[\n  1,\n  2,\n  3,\n  4,\n  5\n]\n"
    upstream = b"[\n  0,\n  2,\n  3,\n  4,\n  5\n]\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == b"[\n  0,\n  2,\n  3,\n  4,\n  5\n]\n"


def test_alias_bearing_yaml_still_merges_adjacent_edits_by_key() -> None:
    fid = file_id("anchors-adjacent")
    host = _ANCHORED + b"svc3:\n  <<: *d\n  name: three\n"
    upstream = _ANCHORED.replace(b"z: 1", b"z: 2")
    _seed(fid, base=_ANCHORED, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == host.replace(b"z: 1", b"z: 2")
    assert out.new_base == upstream


def _assert_converged(fid, out, upstream: bytes, fmt: StructuredFormat) -> None:
    assert isinstance(out.content, bytes)
    _seed(fid, base=upstream, local=out.content)
    again = reconcile_structured_file(
        _P, fid, live=out.content, tracked=upstream, fmt=fmt
    )
    assert again.kind is ReconcileKind.NOOP


_SAME_SPOT_INSERTS = [
    pytest.param(
        _FMT,
        b"a: 1\n",
        b"a: 1\n# host note: keep me\n",
        b"a: 1\nt: 1\n",
        b"a: 1\n# host note: keep me\nt: 1\n",
        id="yaml-host-comment-at-end",
    ),
    pytest.param(
        _FMT,
        b"a: 1\nz: 9\n",
        b"a: 1\n# host note\nz: 9\n",
        b"a: 1\nt: 1\nz: 9\n",
        b"a: 1\n# host note\nt: 1\nz: 9\n",
        id="yaml-host-comment-mid-file",
    ),
    pytest.param(
        _JSON,
        b'{\n  "a": 1,\n  "b": 2\n}\n',
        b'{\n  "a": 1,\n  // host note\n  "b": 2\n}\n',
        b'{\n  "a": 1,\n  "t": 1,\n  "b": 2\n}\n',
        b'{\n  "a": 1,\n  // host note\n  "b": 2,\n  "t": 1\n}\n',
        id="jsonc-host-comment-above-a-key",
    ),
    pytest.param(
        _FMT,
        b"a: 1\nn: null\nb:   2\n",
        b"a: 1\nn: null\nb:   2\nh: 1\n",
        b"a: 1\nn: null\nb:   2\nt: 1\n",
        b"a: 1\nn: null\nb:   2\nh: 1\nt: 1\n",
        id="yaml-both-append",
    ),
    pytest.param(
        _FMT,
        b"a: 1\nn: ~\nz:   2\n",
        b"a: 1\nh: 1\nn: ~\nz:   2\n",
        b"a: 1\nt: 1\nu: 2\nn: ~\nz:   2\n",
        b"a: 1\nh: 1\nt: 1\nu: 2\nn: ~\nz:   2\n",
        id="yaml-both-insert-uneven",
    ),
    pytest.param(
        _JSON,
        b'{\n  "a":   1,\n  "c": 3\n}\n',
        b'{\n  "a":   1,\n  "c": 3,\n  "h": true\n}\n',
        b'{\n  "a":   1,\n  "c": 3,\n  "t": 2\n}\n',
        b'{\n  "a":   1,\n  "c": 3,\n  "h": true,\n  "t": 2\n}\n',
        id="json-both-append",
    ),
]


@pytest.mark.parametrize(
    ("fmt", "base", "host", "upstream", "expected"), _SAME_SPOT_INSERTS
)
def test_both_sides_inserting_at_one_spot_keeps_both_lines(
    fmt: StructuredFormat, base: bytes, host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("same-spot")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=fmt)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream
    _assert_converged(fid, out, upstream, fmt)


_INTERLEAVED = [
    pytest.param(
        _FMT,
        b"k0: 3\nk1: 7\nk2: 1\nn: ~\n",
        b"k0: 3\nk1: 7\n# h note\nk2: 1\nn: ~\n",
        b"k0: 3\nk2: 1\nn: ~\n",
        b"k0: 3\n# h note\nk2: 1\nn: ~\n",
        id="yaml-host-comment-after-a-key-upstream-deletes",
    ),
    pytest.param(
        _JSON,
        b'{\n  "k0": 7,\n  "k1": 7,\n  "k2": 8,\n  "k3": 0\n}\n',
        b'{\n  "k0": 7,\n  "k1": 72,\n  // h note\n  "k2": 8,\n  "k3": 0\n}\n',
        b'{\n  "k0": 7,\n  "k1": 7,\n  "k3": 0\n}\n',
        b'{\n  "k0": 7,\n  "k1": 72,\n  // h note\n  "k3": 0\n}\n',
        id="json-host-comment-above-a-key-upstream-deletes",
    ),
    pytest.param(
        _JSON,
        b'{\n  "k0": 8,\n  "k1": 2\n}\n',
        b'{\n  "k0": 8,\n  "h3": 7,\n  "k1": 2\n  // h note\n}\n',
        b'{\n  "k0": 8,\n  "k1": 16\n}\n',
        b'{\n  "k0": 8,\n  "h3": 7,\n  "k1": 16\n  // h note\n}\n',
        id="json-host-comment-after-the-last-member-upstream-edits",
    ),
    pytest.param(
        _FMT,
        b"k0: 3\nk1: 8\nk2:\n    x: 4\n    y: 9\nn: ~\n",
        b"k0: 3\nk1: 8\nh2: 1\nk2:\n    x: 4\n    y: 9\nn: ~\n",
        b"k0: 3\nk2:\n    x: 4\n    y: 9\nn: ~\n",
        b"k0: 3\nh2: 1\nk2:\n    x: 4\n    y: 9\nn: ~\n",
        id="yaml-host-key-next-to-a-key-upstream-deletes",
    ),
    pytest.param(
        _FMT,
        b"a: 1\nk3: 3\nk4: 7\nn: ~\n",
        b"a: 1\nk4: 7\nn: ~\n",
        b"a: 1\nk3: 3\nt2: 4\nk4: 7\nn: ~\n",
        b"a: 1\nt2: 4\nk4: 7\nn: ~\n",
        id="yaml-upstream-key-next-to-a-key-the-host-deleted",
    ),
    pytest.param(
        _FMT,
        b"a: 1\nk1: 2\nn: ~\n",
        b"a: 1\nh3: 8\nk1: 2\n# h note\nn: ~\n",
        b"a: 1\nk1: 16\nn: ~\n",
        b"a: 1\nh3: 8\nk1: 16\n# h note\nn: ~\n",
        id="yaml-edited-line-pairs-with-its-edit-inside-a-longer-run",
    ),
]


@pytest.mark.parametrize(("fmt", "base", "host", "upstream", "expected"), _INTERLEAVED)
def test_interleaved_edits_keep_every_live_only_line(
    fmt: StructuredFormat, base: bytes, host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("interleaved")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=fmt)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream
    _assert_converged(fid, out, upstream, fmt)


_D1 = b"defaults: &d {retries: 1, timeout: 5}\n"
_D3 = b"defaults: &d {retries: 3, timeout: 5}\n"
_ALIAS = b"prod: *d\n"
_LITERAL = b"prod: {retries: 1, timeout: 5}\n"
_DE_ALIASED = [
    pytest.param(
        _D1 + _ALIAS + b"name: x\n",
        _D1 + _ALIAS + b"name: x\nhost_only: 1\n",
        _D3 + _LITERAL + b"name: x\n",
        _D3 + _LITERAL + b"name: x\nhost_only: 1\n",
        id="upstream-de-aliases-and-changes-the-anchor",
    ),
    pytest.param(
        _D1 + _ALIAS + b"name: x\n",
        _D3 + _LITERAL + b"name: x\n",
        _D1 + _ALIAS + b"name: x\nupstream_only: 1\n",
        _D3 + _LITERAL + b"name: x\nupstream_only: 1\n",
        id="host-de-aliases-and-changes-the-anchor",
    ),
    pytest.param(
        b"name: x\n" + _D1 + _ALIAS,
        b"name: x\n" + _D1 + _ALIAS + b"host_only: 1\n",
        b"name: x\n" + _D3 + _LITERAL,
        b"name: x\n" + _D3 + _LITERAL + b"host_only: 1\n",
        id="upstream-de-aliases-next-to-a-host-append",
    ),
    pytest.param(
        b"name: x\n" + _D1 + _ALIAS,
        b"name: x\n" + _D3 + _LITERAL,
        b"name: x\n" + _D1 + _ALIAS + b"upstream_only: 1\n",
        b"name: x\n" + _D3 + _LITERAL + b"upstream_only: 1\n",
        id="host-de-aliases-next-to-an-upstream-append",
    ),
]


@pytest.mark.parametrize(("base", "host", "upstream", "expected"), _DE_ALIASED)
def test_de_aliased_key_keeps_the_value_its_side_wrote(
    base: bytes, host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("de-aliased")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    assert out.new_base == upstream
    _assert_converged(fid, out, upstream, _FMT)


def test_commented_json_does_not_gain_a_trailing_comma_either() -> None:
    fid = file_id("jsonc-trailing-comma")
    base = b'{\n  "k0": 4,\n  "k4": 3,\n  "k5": 4\n}\n'
    host = b'{\n  "k0": 4,\n  "k4": 3,\n  // h note\n  "k5": 4\n}\n'
    upstream = b'{\n  "k0": 4,\n  "k4": 3\n}\n'
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_JSON)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == b'{\n  "k0": 4,\n  "k4": 3\n  // h note\n}\n'
    _assert_converged(fid, out, upstream, _JSON)


_F3_BASE = b"a: 3\nb: 1\nc: false\n"
_DETERMINISTIC = [
    pytest.param(
        _F3_BASE,
        b"a: 3\nb: 1  # host note\nc: false\n",
        b"a: 3\nt: false\nb: 1\nc: null\n",
        b"a: 3\nt: false\nb: 1  # host note\nc: null\n",
        id="host-comment-on-a-line-between-upstream-edits",
    ),
    pytest.param(
        b"a: 1\nb: 2",
        b"a: 1\nb: 2\n# host note",
        b"a: 1\nb: 3",
        b"a: 1\nb: 3\n# host note",
        id="host-comment-appended-without-final-newline",
    ),
    pytest.param(
        b"a: 1\nb: 2\n",
        b"a: 1\nb: 2\n# host note\n",
        b"a: 1\nb: 3",
        b"a: 1\nb: 3\n# host note\n",
        id="only-upstream-lacks-the-final-newline",
    ),
    pytest.param(
        b"x: 1\ny: 2\nz: ~\n",
        b"x: 5\ny: 2\nz: ~\n",
        b"x: 1\ny: 7\nz: ~\n",
        b"x: 5\ny: 7\nz: ~\n",
        id="adjacent-single-line-edits-by-different-sides",
    ),
    pytest.param(
        b"k: 1\n# upstream note\nm: 2\nz: ~\n",
        b"k: 1\n# upstream note\nm: 2\nh: 1\nz: ~\n",
        b"# upstream note\nm: 3\nz: ~\n",
        b"# upstream note\nm: 3\nh: 1\nz: ~\n",
        id="upstream-deletes-above-a-comment-both-keep",
    ),
    pytest.param(
        b"k: 1\nm: 2\nz: ~\n",
        b"k: 1\nm: 2\nz: ~\n# host tail\n",
        b"k: 1\n# why t\nt: 0\nm: 2\nz: ~\nu: 9\n",
        b"k: 1\n# why t\nt: 0\nm: 2\nz: ~\n# host tail\nu: 9\n",
        id="upstream-comment-comes-once-with-its-key",
    ),
]


@pytest.mark.parametrize(("base", "host", "upstream", "expected"), _DETERMINISTIC)
def test_non_colliding_edits_in_one_hunk_merge_from_the_lines(
    base: bytes, host: bytes, upstream: bytes, expected: bytes
) -> None:
    fid = file_id("one-hunk")
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == expected
    _assert_converged(fid, out, upstream, _FMT)


def test_both_sides_changing_one_line_differently_is_re_serialised() -> None:
    fid = file_id("same-line")
    base = b"b: 1\nl:\n    - x\n"
    host = b"b: 1  # host note\nl:\n    - x\n"
    upstream = b"b: 2\nl:\n    - x\n"
    _seed(fid, base=base, local=host)

    out = reconcile_structured_file(_P, fid, live=host, tracked=upstream, fmt=_FMT)

    assert out.kind is ReconcileKind.WRITE
    assert out.content == b"b: 2\nl:\n    - x\n"


def _reformatted(lines: int, fmt: StructuredFormat) -> tuple[bytes, bytes, bytes]:
    """A document where the host edits one value and upstream reformats it all."""
    values = {f"key{n}": n for n in range(lines)}
    if fmt is _FMT:
        base = "".join(f"{key}: {value}\n" for key, value in values.items()).encode()
        host = base.replace(b"key7: 7\n", b"key7: 77\n")
        upstream = base.replace(b"key9: 9\n", b"key9: 99\n").replace(b"\n", b"\r\n")
        return base, host, upstream
    import json

    base = (json.dumps(values, indent=2) + "\n").encode()
    host = base.replace(b'"key7": 7,', b'"key7": 77,')
    values["key9"] = 99
    return base, host, (json.dumps(values, indent=4) + "\n").encode()


@pytest.mark.parametrize("fmt", [_FMT, _JSON])
@pytest.mark.parametrize("lines", [1200, 5000])
def test_whole_file_reformat_costs_a_fixed_number_of_parses(
    monkeypatch: pytest.MonkeyPatch, fmt: StructuredFormat, lines: int
) -> None:
    import time

    from setforge import reconcile_apply
    from setforge.reconcile import structured_units

    base, host, upstream = _reformatted(lines, fmt)
    fid = file_id("reformatted")
    _seed(fid, base=base, local=host)
    parses: list[int] = []
    real_load = structured_units._load_model

    def counting_load(data: bytes, load_fmt: StructuredFormat) -> object:
        parses.append(len(data))
        return real_load(data, load_fmt)

    monkeypatch.setattr(structured_units, "_load_model", counting_load)
    started = time.monotonic()

    out = reconcile_apply.reconcile_structured_file(
        _P, fid, live=host, tracked=upstream, fmt=fmt
    )

    elapsed = time.monotonic() - started
    assert out.kind is ReconcileKind.WRITE
    assert isinstance(out.content, bytes)
    merged = real_load(out.content, fmt)
    assert structured_units.get_at_path(merged, "key7") == 77
    assert structured_units.get_at_path(merged, "key9") == 99
    assert len(parses) <= 4
    assert elapsed < 60


def test_checking_a_candidate_with_a_reused_anchor_does_not_warn() -> None:
    import warnings

    from setforge.reconcile_apply import _parses

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _parses(b"a: &d 1\nb: &d 2\nc: *d\n", _FMT) is True


@pytest.mark.parametrize(
    ("base", "ours", "theirs", "expected"),
    [
        pytest.param(
            [],
            [b"h: 1\n", b"same: 0\n"],
            [b"same: 0\n", b"t: 1\n"],
            [b"h: 1\n", b"same: 0\n", b"t: 1\n"],
            id="both-insert-at-one-spot",
        ),
        pytest.param(
            [b"b: 2\n"],
            [b"# above\n", b"b: 2\n", b"# below\n"],
            [b"b: 3\n", b"t: 1\n"],
            [b"# above\n", b"b: 3\n", b"t: 1\n", b"# below\n"],
            id="one-side-only-adds-around-the-base-lines",
        ),
        pytest.param(
            [b"x: 1\n", b"y: 2\n", b"z: 3\n"],
            [b"x: 5\n", b"y: 2\n", b"z: 9\n"],
            [b"x: 1\n", b"y: 7\n", b"z: 9\n"],
            [b"x: 5\n", b"y: 7\n", b"z: 9\n"],
            id="line-by-line-with-one-identical-change",
        ),
        pytest.param(
            [b"x: 1\n", b"y: 2\n"],
            [b"y: 2\n", b"h: 1\n"],
            [b"x: 1\n", b"y: 3\n"],
            [b"y: 3\n", b"h: 1\n"],
            id="delete-edit-and-insert-on-different-lines",
        ),
        pytest.param(
            [b"x: 1\n"], [b"x: 2\n"], [b"x: 3\n"], None, id="same-line-changed-twice"
        ),
        pytest.param(
            [b"x: 1\n", b"y: 2\n", b"z: 3\n"],
            [b"x: 1\n", b"h: 1\n", b"y: 2\n", b"z: 3\n"],
            [b"new: 0\n"],
            None,
            id="insertion-inside-a-replaced-run",
        ),
    ],
)
def test_resolve_hunk_applies_edits_that_do_not_touch_the_same_line(
    base: list[bytes],
    ours: list[bytes],
    theirs: list[bytes],
    expected: list[bytes] | None,
) -> None:
    from setforge.reconcile_apply import _resolve_hunk

    assert _resolve_hunk(base, ours, theirs) == expected


@pytest.mark.parametrize(("pairs", "resolved"), [(64, True), (65, False)])
def test_resolve_hunk_gives_up_past_a_fixed_number_of_edit_pairs(
    pairs: int, resolved: bool
) -> None:
    from setforge.reconcile_apply import _resolve_hunk

    base = [b"k%d: 0\n" % n for n in range(2 * pairs)]
    ours = [line.replace(b"0", b"1") if n % 2 else line for n, line in enumerate(base)]
    theirs = [
        line if n % 2 else line.replace(b"0", b"2") for n, line in enumerate(base)
    ]

    merged = _resolve_hunk(base, ours, theirs)

    assert (merged is not None) is resolved
    if resolved:
        assert merged == [
            line.replace(b"0", b"1" if n % 2 else b"2") for n, line in enumerate(base)
        ]
