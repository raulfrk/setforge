"""Tests for the non-interactive disposition merge driver.

Covers the disposition x auto x base-presence matrix that
:func:`setforge.disposition_merge.resolve_file` maps to deployed text and a
re-baseline decision, across markdown / YAML / JSONC formats.
"""

from pathlib import Path

import pytest
from json5.loader import loads as _json5_loads
from ruamel.yaml import YAML

from setforge.config import Disposition
from setforge.disposition_merge import FileResolution, resolve_file
from setforge.markdown_merge import LineConflict
from setforge.section_wizard import ReconcileAuto
from setforge.structural_merge import PathConflict


def _yaml_load(text: str) -> object:
    """Parse YAML text to a plain python value for assertion."""
    return YAML(typ="safe").load(text)


# ---------------------------------------------------------------------------
# 1. PINNED.
# ---------------------------------------------------------------------------


def test_pinned_returns_live_verbatim() -> None:
    live = "alpha\nbeta\n"
    res = resolve_file(
        Disposition.PINNED,
        Path("note.md"),
        base="old\n",
        live=live,
        tracked="tracked\n",
        auto=None,
    )
    assert res == FileResolution(
        text=live, conflicts=[], advance_base=False, base_absent=False
    )


def test_pinned_ignores_yaml_and_jsonc() -> None:
    live = "a: 1\n"
    res = resolve_file(
        Disposition.PINNED,
        Path("c.yaml"),
        base="a: 0\n",
        live=live,
        tracked="a: 9\n",
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert res.text == live
    assert res.advance_base is False


# ---------------------------------------------------------------------------
# 2. base absent -> 2-way fallback (deploy tracked verbatim).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tracked"),
    [
        ("note.md", "line one\nline two\n"),
        ("conf.yaml", "key: value\n"),
        ("settings.json", '{\n  "k": 1\n}\n'),
    ],
)
def test_base_absent_deploys_tracked(name: str, tracked: str) -> None:
    res = resolve_file(
        Disposition.SHARED,
        Path(name),
        base=None,
        live="something different\n",
        tracked=tracked,
        auto=None,
    )
    assert res.text == tracked
    assert res.conflicts == []
    assert res.advance_base is True
    assert res.base_absent is True


# ---------------------------------------------------------------------------
# 3. clean merge: non-overlapping changes on both sides.
# ---------------------------------------------------------------------------


def test_shared_markdown_clean_non_overlapping() -> None:
    base = "title\nbody\nfooter\n"
    live = "TITLE\nbody\nfooter\n"  # changed first line
    tracked = "title\nbody\nFOOTER\n"  # changed last line
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts == []
    assert res.advance_base is True
    assert res.base_absent is False
    assert "TITLE" in res.text
    assert "FOOTER" in res.text


def test_shared_yaml_clean_non_overlapping() -> None:
    base = "a: 1\nb: 2\n"
    live = "a: 10\nb: 2\n"  # changed a
    tracked = "a: 1\nb: 20\n"  # changed b
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts == []
    assert res.advance_base is True
    loaded = _yaml_load(res.text)
    assert loaded == {"a": 10, "b": 20}


def test_shared_jsonc_clean_non_overlapping() -> None:
    base = '{\n  "a": 1,\n  "b": 2\n}\n'
    live = '{\n  "a": 10,\n  "b": 2\n}\n'
    tracked = '{\n  "a": 1,\n  "b": 20\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts == []
    assert res.advance_base is True
    assert _json5_loads(res.text) == {"a": 10, "b": 20}


# ---------------------------------------------------------------------------
# 4. conflict + KEEP_LIVE -> ours wins; advance_base True.
# ---------------------------------------------------------------------------


def test_shared_markdown_conflict_keep_live() -> None:
    base = "value\n"
    live = "live-value\n"
    tracked = "tracked-value\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.KEEP_LIVE,
    )
    assert res.conflicts
    assert all(isinstance(c, LineConflict) for c in res.conflicts)
    assert "live-value" in res.text
    assert "tracked-value" not in res.text
    assert res.advance_base is True


def test_shared_yaml_conflict_keep_live() -> None:
    base = "a: 1\n"
    live = "a: 2\n"
    tracked = "a: 3\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.KEEP_LIVE,
    )
    assert res.conflicts
    assert all(isinstance(c, PathConflict) for c in res.conflicts)
    assert _yaml_load(res.text) == {"a": 2}
    assert res.advance_base is True


def test_shared_jsonc_conflict_keep_live() -> None:
    base = '{\n  "a": 1\n}\n'
    live = '{\n  "a": 2\n}\n'
    tracked = '{\n  "a": 3\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.KEEP_LIVE,
    )
    assert res.conflicts
    assert _json5_loads(res.text) == {"a": 2}
    assert res.advance_base is True


# ---------------------------------------------------------------------------
# 5. conflict + USE_TRACKED -> theirs wins; advance_base True.
# ---------------------------------------------------------------------------


def test_shared_markdown_conflict_use_tracked() -> None:
    base = "value\n"
    live = "live-value\n"
    tracked = "tracked-value\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert res.conflicts
    assert "tracked-value" in res.text
    assert "live-value" not in res.text
    assert res.advance_base is True


def test_shared_yaml_conflict_use_tracked() -> None:
    base = "a: 1\n"
    live = "a: 2\n"
    tracked = "a: 3\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert res.conflicts
    assert _yaml_load(res.text) == {"a": 3}
    assert res.advance_base is True


def test_shared_jsonc_conflict_use_tracked() -> None:
    base = '{\n  "a": 1\n}\n'
    live = '{\n  "a": 2\n}\n'
    tracked = '{\n  "a": 3\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert res.conflicts
    assert _json5_loads(res.text) == {"a": 3}
    assert res.advance_base is True


# ---------------------------------------------------------------------------
# 6. conflict + auto None -> ours wins in text BUT advance_base False (defer).
# ---------------------------------------------------------------------------


def test_shared_markdown_conflict_bare_defers() -> None:
    base = "value\n"
    live = "live-value\n"
    tracked = "tracked-value\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts
    assert "live-value" in res.text
    assert res.advance_base is False


def test_shared_yaml_conflict_bare_defers() -> None:
    base = "a: 1\n"
    live = "a: 2\n"
    tracked = "a: 3\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts
    assert _yaml_load(res.text) == {"a": 2}
    assert res.advance_base is False


def test_shared_jsonc_conflict_bare_defers() -> None:
    base = '{\n  "a": 1\n}\n'
    live = '{\n  "a": 2\n}\n'
    tracked = '{\n  "a": 3\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert res.conflicts
    assert _json5_loads(res.text) == {"a": 2}
    assert res.advance_base is False


# ---------------------------------------------------------------------------
# 7. forked behaves like shared for the merge itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auto", [None, ReconcileAuto.KEEP_LIVE, ReconcileAuto.USE_TRACKED]
)
def test_forked_merge_parity_with_shared_markdown(auto: ReconcileAuto | None) -> None:
    base = "value\n"
    live = "live-value\n"
    tracked = "tracked-value\n"
    shared = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=auto,
    )
    forked = resolve_file(
        Disposition.FORKED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=auto,
    )
    assert forked.text == shared.text
    assert forked.advance_base == shared.advance_base
    assert len(forked.conflicts) == len(shared.conflicts)


def test_forked_merge_parity_with_shared_yaml() -> None:
    base = "a: 1\nb: 2\n"
    live = "a: 10\nb: 2\n"
    tracked = "a: 1\nb: 20\n"
    shared = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    forked = resolve_file(
        Disposition.FORKED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert _yaml_load(forked.text) == _yaml_load(shared.text)
    assert forked.advance_base == shared.advance_base


# ---------------------------------------------------------------------------
# 8. structural round-trip: comment on untouched key survives.
# ---------------------------------------------------------------------------


def test_shared_yaml_preserves_comment_on_untouched_key() -> None:
    base = "a: 1\nb: 2  # keep me\n"
    live = "a: 10\nb: 2  # keep me\n"
    tracked = "a: 1\nb: 2  # keep me\n"
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert "# keep me" in res.text
    assert _yaml_load(res.text) == {"a": 10, "b": 2}


def test_shared_jsonc_preserves_comment_on_untouched_key() -> None:
    base = '{\n  "a": 1,\n  "b": 2 // keep me\n}\n'
    live = '{\n  "a": 10,\n  "b": 2 // keep me\n}\n'
    tracked = '{\n  "a": 1,\n  "b": 2 // keep me\n}\n'
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    assert "// keep me" in res.text
    assert _json5_loads(res.text) == {"a": 10, "b": 2}


# ---------------------------------------------------------------------------
# 9. shape mismatch falls back to line-based merge without raising.
# ---------------------------------------------------------------------------


def test_shape_mismatch_falls_back_to_line_merge() -> None:
    base = "a:\n  nested: 1\n"
    live = "a:\n  nested: 2\n"  # still a map, changed
    tracked = "a: scalar\n"  # now a scalar -> shape mismatch
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
    )
    # Did not raise; produced text via the line-based fallback.
    assert isinstance(res, FileResolution)
    assert isinstance(res.text, str)
    # A conflict at the diverged region; conflicts are LineConflict (line path).
    assert all(isinstance(c, LineConflict) for c in res.conflicts)
