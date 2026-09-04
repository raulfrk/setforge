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
