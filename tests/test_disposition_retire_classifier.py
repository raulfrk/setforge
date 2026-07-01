"""Wave-3 tests: the disposition/span -> per-unit SHARED/LOCAL classifier.

Pins the mapping fidelity rules the cutover turns on: forked/pinned -> all
divergent units LOCAL; shared structured + span -> only span-covered key-units
LOCAL (incl. deep subtree); shared -> lazy (no pre-classified rows); DL1 base is
the verbatim tracked bytes; and the present-null vs absent trichotomy produces
distinct units (never collapses).
"""

from __future__ import annotations

from pathlib import Path

from setforge.migrations._disposition_retire import (
    _classify_fid,
    _FidLegacy,
    _SpanSpec,
)
from setforge.reconcile import file_id
from setforge.reconcile.types import ABSENT

_YAML_BASE = b"editor:\n  fontSize: 12\n"
_YAML_LIVE = b"editor:\n  fontSize: 18\n"


def _rec(
    *,
    dst: str,
    disposition: str | None = None,
    spans: tuple[_SpanSpec, ...] = (),
    tracked: bytes = _YAML_BASE,
    live: bytes = _YAML_LIVE,
    dst_exists: bool = True,
    is_structured: bool = True,
) -> _FidLegacy:
    return _FidLegacy(
        profile="default",
        name="f",
        fid=file_id("f"),
        src=Path("/repo/tracked/f"),
        dst=Path(dst),
        disposition=disposition,
        spans=spans,
        tracked_bytes=tracked,
        live_bytes=live,
        dst_exists=dst_exists,
        is_structured=is_structured,
    )


def _span(anchor: str, *, deep: bool = False) -> _SpanSpec:
    return _SpanSpec(anchor=anchor, deep=deep, kind="pinned", semantics="host-local")


def test_shared_structured_no_span_is_lazy() -> None:
    plan = _classify_fid(_rec(dst="c.yaml", disposition="shared"))
    assert plan.hunks is None
    assert plan.base == _YAML_BASE  # DL1: verbatim tracked bytes
    assert plan.local == _YAML_LIVE


def test_forked_structured_all_units_local() -> None:
    plan = _classify_fid(_rec(dst="c.yaml", disposition="forked"))
    assert plan.hunks is not None
    assert [r["cls"] for r in plan.hunks] == ["local"]
    row = plan.hunks[0]
    assert row["kind"] == "key"
    assert row["path"] == "editor.fontSize"


def test_pinned_structured_all_units_local() -> None:
    plan = _classify_fid(_rec(dst="c.yaml", disposition="pinned"))
    assert [r["cls"] for r in plan.hunks or []] == ["local"]


def test_shared_structured_exact_span_only_covers_that_key() -> None:
    plan = _classify_fid(
        _rec(
            dst="c.yaml",
            disposition="shared",
            spans=(_span("editor.fontSize"),),
        )
    )
    assert plan.hunks is not None
    assert plan.hunks[0]["path"] == "editor.fontSize"
    assert plan.hunks[0]["cls"] == "local"


def test_shared_structured_deep_span_covers_subtree() -> None:
    plan = _classify_fid(
        _rec(
            dst="c.yaml",
            disposition="shared",
            spans=(_span("editor", deep=True),),
        )
    )
    assert plan.hunks is not None
    assert plan.hunks[0]["path"] == "editor.fontSize"


def test_shared_structured_nonmatching_span_is_lazy() -> None:
    plan = _classify_fid(
        _rec(
            dst="c.yaml",
            disposition="shared",
            spans=(_span("other.key"),),
        )
    )
    assert plan.hunks is None


def test_forked_markdown_all_hunks_local() -> None:
    plan = _classify_fid(
        _rec(
            dst="NOTES.md",
            disposition="forked",
            tracked=b"# t\nalpha\n",
            live=b"# t\nbeta\n",
            is_structured=False,
        )
    )
    assert plan.hunks is not None
    assert all(r["cls"] == "local" for r in plan.hunks)
    # Line rows omit "kind" (index_model defaults an absent kind to "line",
    # unlike structured rows which set kind:"key").
    assert all(r.get("kind", "line") == "line" for r in plan.hunks)


def test_shared_markdown_is_lazy() -> None:
    plan = _classify_fid(
        _rec(
            dst="NOTES.md",
            disposition="shared",
            tracked=b"# t\nalpha\n",
            live=b"# t\nbeta\n",
            is_structured=False,
        )
    )
    assert plan.hunks is None


def test_undeployed_dst_seeds_absent_local_and_no_hunks() -> None:
    plan = _classify_fid(_rec(dst="c.yaml", disposition="forked", dst_exists=False))
    assert plan.local is ABSENT
    assert plan.hunks is None
    assert plan.base == _YAML_BASE


def test_present_null_and_absent_do_not_collapse() -> None:
    # A key deployed as literal null vs a key removed live must not produce the
    # same unit identity (the trichotomy-collapse pitfall).
    null_plan = _classify_fid(
        _rec(
            dst="c.yaml",
            disposition="forked",
            live=b"editor:\n  fontSize: null\n",
        )
    )
    absent_plan = _classify_fid(
        _rec(dst="c.yaml", disposition="forked", live=b"editor: {}\n")
    )
    assert null_plan.hunks is not None
    null_hash = null_plan.hunks[0]["value_hash"]
    absent_rows = [
        r for r in (absent_plan.hunks or []) if r["path"] == "editor.fontSize"
    ]
    # present-null yields a fontSize unit; live-absent yields either no fontSize
    # unit or one with a distinct value_hash — never the same identity as null.
    if absent_rows:
        assert absent_rows[0]["value_hash"] != null_hash
