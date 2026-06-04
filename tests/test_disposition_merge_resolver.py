"""Tests for the injectable per-conflict resolver in the disposition driver.

Exercises :func:`setforge.disposition_merge.resolve_file` driven by an injected
:data:`~setforge.disposition_merge.ConflictResolver` across a line-based
(markdown) fixture and a structural (YAML / JSONC) fixture. Each conflict is
driven through the resolver one of four ways
(:class:`~setforge.disposition_merge.ConflictChoice`) and the any-skip-defers
re-baseline rule is asserted, alongside a ``resolver=None`` regression check.
"""

from pathlib import Path

import pytest
from json5.loader import loads as _json5_loads
from ruamel.yaml import YAML

from setforge.config import Disposition
from setforge.disposition_merge import (
    ConflictChoice,
    ConflictResolution,
    ConflictResolver,
    FileResolution,
    resolve_file,
)
from setforge.markdown_merge import LineConflict
from setforge.section_wizard import ReconcileAuto
from setforge.structural_merge import PathConflict


def _yaml_load(text: str) -> object:
    """Parse YAML text to a plain python value for assertion."""
    return YAML(typ="safe").load(text)


# Single same-region / same-key conflict fixtures.
_MD = ("value\n", "live-value\n", "tracked-value\n")  # base, live, tracked
_YAML = ("a: 1\n", "a: 2\n", "a: 3\n")
_JSONC = ('{\n  "a": 1\n}\n', '{\n  "a": 2\n}\n', '{\n  "a": 3\n}\n')


def _const_resolver(res: ConflictResolution) -> ConflictResolver:
    """A resolver that returns ``res`` for every conflict."""

    def _resolve(_conflict: LineConflict | PathConflict) -> ConflictResolution:
        return res

    return _resolve


# ---------------------------------------------------------------------------
# 1. KEEP_OURS -> text has ours; advance_base True; conflicts non-empty.
# ---------------------------------------------------------------------------


def test_keep_ours_markdown() -> None:
    base, live, tracked = _MD
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(ConflictResolution(ConflictChoice.KEEP_OURS)),
    )
    assert "live-value" in res.text
    assert "tracked-value" not in res.text
    assert res.advance_base is True
    assert res.conflicts


def test_keep_ours_yaml() -> None:
    base, live, tracked = _YAML
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(ConflictResolution(ConflictChoice.KEEP_OURS)),
    )
    assert _yaml_load(res.text) == {"a": 2}
    assert res.advance_base is True
    assert res.conflicts


# ---------------------------------------------------------------------------
# 2. TAKE_THEIRS -> text has theirs; advance_base True.
# ---------------------------------------------------------------------------


def test_take_theirs_markdown() -> None:
    base, live, tracked = _MD
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(ConflictResolution(ConflictChoice.TAKE_THEIRS)),
    )
    assert "tracked-value" in res.text
    assert "live-value" not in res.text
    assert res.advance_base is True


def test_take_theirs_jsonc() -> None:
    base, live, tracked = _JSONC
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(ConflictResolution(ConflictChoice.TAKE_THEIRS)),
    )
    assert _json5_loads(res.text) == {"a": 3}
    assert res.advance_base is True


# ---------------------------------------------------------------------------
# 3. EDIT -> text has the edited content; advance_base True.
# ---------------------------------------------------------------------------


def test_edit_markdown() -> None:
    base, live, tracked = _MD
    edit = ConflictResolution(ConflictChoice.EDIT, edited_lines=["edited-value\n"])
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(edit),
    )
    assert "edited-value" in res.text
    assert "live-value" not in res.text
    assert "tracked-value" not in res.text
    assert res.advance_base is True


def test_edit_yaml() -> None:
    base, live, tracked = _YAML
    edit = ConflictResolution(ConflictChoice.EDIT, edited_value=99)
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(edit),
    )
    assert _yaml_load(res.text) == {"a": 99}
    assert res.advance_base is True


# ---------------------------------------------------------------------------
# 4. SKIP -> text keeps ours; advance_base False (defer).
# ---------------------------------------------------------------------------


def test_skip_markdown_defers() -> None:
    base, live, tracked = _MD
    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(ConflictResolution(ConflictChoice.SKIP)),
    )
    assert "live-value" in res.text
    assert res.advance_base is False
    assert res.conflicts


def test_skip_yaml_defers() -> None:
    base, live, tracked = _YAML
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(ConflictResolution(ConflictChoice.SKIP)),
    )
    assert _yaml_load(res.text) == {"a": 2}
    assert res.advance_base is False
    assert res.conflicts


# ---------------------------------------------------------------------------
# 5. MIXED: TAKE_THEIRS + SKIP -> take-theirs applied, but any-skip defers.
# ---------------------------------------------------------------------------


def test_mixed_markdown_take_theirs_and_skip() -> None:
    # Two disjoint conflicting regions in one document.
    base = "top\nMID\nbottom\n"
    live = "top-live\nMID\nbottom-live\n"
    tracked = "top-tracked\nMID\nbottom-tracked\n"

    seen: list[LineConflict | PathConflict] = []

    def _resolve(conflict: LineConflict | PathConflict) -> ConflictResolution:
        seen.append(conflict)
        # First conflict take theirs, second skip.
        if len(seen) == 1:
            return ConflictResolution(ConflictChoice.TAKE_THEIRS)
        return ConflictResolution(ConflictChoice.SKIP)

    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_resolve,
    )
    assert len(seen) == 2
    # First (top) conflict resolved to theirs.
    assert "top-tracked" in res.text
    assert "top-live" not in res.text
    # Second (bottom) conflict skipped -> keeps ours.
    assert "bottom-live" in res.text
    assert "bottom-tracked" not in res.text
    # Any skip defers re-baselining.
    assert res.advance_base is False
    assert len(res.conflicts) == 2


def test_mixed_yaml_take_theirs_and_skip() -> None:
    base = "a: 1\nb: 1\n"
    live = "a: 2\nb: 2\n"
    tracked = "a: 3\nb: 3\n"

    seen: list[str] = []

    def _resolve(conflict: LineConflict | PathConflict) -> ConflictResolution:
        assert isinstance(conflict, PathConflict)
        seen.append(conflict.path)
        if conflict.path == "a":
            return ConflictResolution(ConflictChoice.TAKE_THEIRS)
        return ConflictResolution(ConflictChoice.SKIP)

    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_resolve,
    )
    loaded = _yaml_load(res.text)
    # a took theirs; b skipped -> ours.
    assert loaded == {"a": 3, "b": 2}
    assert res.advance_base is False
    assert len(res.conflicts) == 2


# ---------------------------------------------------------------------------
# 6. resolver=None -> byte-identical to the existing auto behavior.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auto", [None, ReconcileAuto.KEEP_LIVE, ReconcileAuto.USE_TRACKED]
)
@pytest.mark.parametrize(
    ("name", "triple"),
    [
        ("d.md", _MD),
        ("c.yaml", _YAML),
        ("s.json", _JSONC),
    ],
)
def test_resolver_none_byte_identical(
    auto: ReconcileAuto | None,
    name: str,
    triple: tuple[str, str, str],
) -> None:
    base, live, tracked = triple
    without = resolve_file(
        Disposition.SHARED,
        Path(name),
        base=base,
        live=live,
        tracked=tracked,
        auto=auto,
    )
    explicit_none = resolve_file(
        Disposition.SHARED,
        Path(name),
        base=base,
        live=live,
        tracked=tracked,
        auto=auto,
        resolver=None,
    )
    assert explicit_none == without


# ---------------------------------------------------------------------------
# 7. resolver called exactly once per conflict, in document order.
# ---------------------------------------------------------------------------


def test_resolver_called_once_per_conflict_in_order_markdown() -> None:
    base = "top\nMID\nbottom\n"
    live = "top-live\nMID\nbottom-live\n"
    tracked = "top-tracked\nMID\nbottom-tracked\n"

    calls: list[LineConflict | PathConflict] = []

    def _record(conflict: LineConflict | PathConflict) -> ConflictResolution:
        calls.append(conflict)
        return ConflictResolution(ConflictChoice.KEEP_OURS)

    res = resolve_file(
        Disposition.SHARED,
        Path("d.md"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_record,
    )
    # One call per conflict, same objects, same order as res.conflicts.
    assert calls == res.conflicts
    assert all(isinstance(c, LineConflict) for c in calls)
    # Document order: top region precedes bottom region. The final line's
    # terminator is stripped by the splitter, so the bottom block lacks "\n".
    assert calls[0].ours == ["top-live\n"]
    assert calls[1].ours == ["bottom-live"]


def test_resolver_called_once_per_conflict_in_order_yaml() -> None:
    base = "a: 1\nb: 1\n"
    live = "a: 2\nb: 2\n"
    tracked = "a: 3\nb: 3\n"

    calls: list[LineConflict | PathConflict] = []

    def _record(conflict: LineConflict | PathConflict) -> ConflictResolution:
        calls.append(conflict)
        return ConflictResolution(ConflictChoice.KEEP_OURS)

    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_record,
    )
    assert calls == res.conflicts
    assert [c.path for c in calls if isinstance(c, PathConflict)] == ["a", "b"]


# ---------------------------------------------------------------------------
# Typing-surface smoke: the alias is callable-shaped.
# ---------------------------------------------------------------------------


def test_conflict_resolver_alias_is_callable_shaped() -> None:
    fn: ConflictResolver = _const_resolver(ConflictResolution(ConflictChoice.KEEP_OURS))
    assert callable(fn)
    out = fn(LineConflict(base=[], ours=["x\n"], theirs=["y\n"]))
    assert isinstance(out, ConflictResolution)
    assert out.choice is ConflictChoice.KEEP_OURS
    # FileResolution remains importable / unchanged in surface.
    assert FileResolution.__name__ == "FileResolution"
