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
