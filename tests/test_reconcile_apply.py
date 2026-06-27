"""Tests for setforge/reconcile_apply.py — the plain-file reconcile core.

Exercises every :class:`ReconcileOutcome` branch against a sandboxed store
(``SETFORGE_STATE_DIR`` redirected to tmp). The conflict branches monkeypatch
``resolve_conflicts`` so the helper's outcome-mapping is unit-tested without
driving the interactive prompt_toolkit wizard (that has its own coverage in
tests/reconcile/test_wizard.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from setforge import reconcile_apply
from setforge.reconcile import (
    ABSENT,
    Clean,
    FileId,
    MergeResult,
    WizardResult,
    file_id,
    write_base,
)
from setforge.reconcile.wizard import CANCEL
from setforge.reconcile_apply import (
    ReconcileKind,
    reconcile_plain_file,
)

_PROFILE = "test-apply"


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


def _fid(name: str = "doc") -> FileId:
    return file_id(name)


class TestCleanOutcomes:
    def test_first_install_writes_and_records(self) -> None:
        # No base, no live file: the tracked content is a clean add.
        out = reconcile_plain_file(_PROFILE, _fid(), live=ABSENT, tracked=b"hello\n")
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"hello\n"
        assert out.new_base == b"hello\n"

    def test_clean_reinstall_is_noop(self) -> None:
        fid = _fid()
        write_base(_PROFILE, fid, b"hello\n")
        out = reconcile_plain_file(_PROFILE, fid, live=b"hello\n", tracked=b"hello\n")
        assert out.kind is ReconcileKind.NOOP
        assert out.content is None
        assert out.new_base is None

    def test_upstream_change_fast_forwards(self) -> None:
        fid = _fid()
        write_base(_PROFILE, fid, b"v1\n")
        out = reconcile_plain_file(_PROFILE, fid, live=b"v1\n", tracked=b"v2\n")
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"v2\n"
        assert out.new_base == b"v2\n"

    def test_local_edit_with_no_upstream_change_is_noop(self) -> None:
        # ours diverged from base, theirs == base: keep the local edit, do
        # not rewrite it, do not re-baseline.
        fid = _fid()
        write_base(_PROFILE, fid, b"orig\n")
        out = reconcile_plain_file(_PROFILE, fid, live=b"edited\n", tracked=b"orig\n")
        assert out.kind is ReconcileKind.NOOP


class TestConflictOutcomes:
    def _setup_conflict(self) -> FileId:
        fid = _fid()
        write_base(_PROFILE, fid, b"base\n")
        return fid

    def test_cancelled_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fid = self._setup_conflict()
        monkeypatch.setattr(
            reconcile_apply, "resolve_conflicts", lambda *a, **k: CANCEL
        )
        out = reconcile_plain_file(_PROFILE, fid, live=b"ours\n", tracked=b"theirs\n")
        assert out.kind is ReconcileKind.CANCELLED
        assert out.content is None
        assert out.new_base is None

    def test_deferred_does_not_rebaseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fid = self._setup_conflict()
        deferred = WizardResult(MergeResult((Clean(b"ours\n"),)), deferred=True)
        monkeypatch.setattr(
            reconcile_apply, "resolve_conflicts", lambda *a, **k: deferred
        )
        out = reconcile_plain_file(_PROFILE, fid, live=b"ours\n", tracked=b"theirs\n")
        assert out.kind is ReconcileKind.DEFERRED
        assert out.new_base is None

    def test_resolved_conflict_writes_and_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fid = self._setup_conflict()
        resolved = WizardResult(MergeResult((Clean(b"merged\n"),)), deferred=False)
        monkeypatch.setattr(
            reconcile_apply, "resolve_conflicts", lambda *a, **k: resolved
        )
        out = reconcile_plain_file(_PROFILE, fid, live=b"ours\n", tracked=b"theirs\n")
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"merged\n"
        # base advances to tracked (theirs), not to the merged content.
        assert out.new_base == b"theirs\n"
