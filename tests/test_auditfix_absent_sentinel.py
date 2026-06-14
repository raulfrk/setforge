"""Regression tests: ABSENT-sentinel (tracked-deleted key) reconcile crash.

When tracked DELETED a key that live also modified, the structural merge
produces a :class:`~setforge.structural_merge.PathConflict` whose ``theirs`` is
the :data:`~setforge.scalar_merge.ABSENT` sentinel. Resolving such a conflict
toward tracked (``auto=USE_TRACKED`` or a resolver ``TAKE_THEIRS`` / ``EDIT``)
must DELETE the key, not write the sentinel into the comment-preserving model —
writing it makes the structural dump raise (``RepresenterError`` for YAML,
``TypeError`` for JSONC) and aborts the install mid-deploy.

Covers both the non-interactive auto path
(:func:`setforge.disposition_merge.resolve_file` with ``auto=USE_TRACKED``) and
the injected-resolver path (``TAKE_THEIRS`` / ``EDIT``) across YAML and JSONC.
"""

from pathlib import Path

from json5.loader import loads as _json5_loads
from ruamel.yaml import YAML

from setforge.config import Disposition
from setforge.disposition_merge import (
    ConflictChoice,
    ConflictResolution,
    ConflictResolver,
    resolve_file,
)
from setforge.markdown_merge import LineConflict
from setforge.scalar_merge import ABSENT, ScalarConflict
from setforge.section_wizard import ReconcileAuto
from setforge.structural_merge import PathConflict


def _yaml_load(text: str) -> object:
    """Parse YAML text to a plain python value for assertion."""
    return YAML(typ="safe").load(text)


def _const_resolver(res: ConflictResolution) -> ConflictResolver:
    """A resolver that returns ``res`` for every conflict."""

    def _resolve(
        _conflict: LineConflict | PathConflict | ScalarConflict,
    ) -> ConflictResolution:
        return res

    return _resolve


# base HAD k; live edited it; tracked DELETED it -> PathConflict.theirs is ABSENT.
_YAML_DELETED = ("k: x\n", "k: y\n", "{}\n")  # base, live, tracked
_JSONC_DELETED = ('{"k": "x"}\n', '{"k": "y"}\n', "{}\n")


# ---------------------------------------------------------------------------
# auto=USE_TRACKED: take-theirs means DELETE the key, not write the sentinel.
# ---------------------------------------------------------------------------


def test_shared_yaml_conflict_use_tracked_theirs_deleted() -> None:
    base, live, tracked = _YAML_DELETED
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert _yaml_load(res.text) == {}
    assert res.conflicts
    assert res.advance_base is True


def test_shared_jsonc_conflict_use_tracked_theirs_deleted() -> None:
    base, live, tracked = _JSONC_DELETED
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=ReconcileAuto.USE_TRACKED,
    )
    assert _json5_loads(res.text) == {}
    assert res.conflicts
    assert res.advance_base is True


# ---------------------------------------------------------------------------
# resolver TAKE_THEIRS: same — DELETE the upstream-removed key.
# ---------------------------------------------------------------------------


def test_resolver_take_theirs_yaml_theirs_deleted() -> None:
    base, live, tracked = _YAML_DELETED
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(ConflictResolution(ConflictChoice.TAKE_THEIRS)),
    )
    assert _yaml_load(res.text) == {}
    assert res.advance_base is True


def test_resolver_take_theirs_jsonc_theirs_deleted() -> None:
    base, live, tracked = _JSONC_DELETED
    res = resolve_file(
        Disposition.SHARED,
        Path("s.json"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(ConflictResolution(ConflictChoice.TAKE_THEIRS)),
    )
    assert _json5_loads(res.text) == {}
    assert res.advance_base is True


# ---------------------------------------------------------------------------
# resolver EDIT with edited_value=ABSENT: same delete guard applies.
# ---------------------------------------------------------------------------


def test_resolver_edit_absent_value_yaml_deletes_key() -> None:
    base, live, tracked = _YAML_DELETED
    edit = ConflictResolution(ConflictChoice.EDIT, edited_value=ABSENT)
    res = resolve_file(
        Disposition.SHARED,
        Path("c.yaml"),
        base=base,
        live=live,
        tracked=tracked,
        auto=None,
        resolver=_const_resolver(edit),
    )
    assert _yaml_load(res.text) == {}
    assert res.advance_base is True
