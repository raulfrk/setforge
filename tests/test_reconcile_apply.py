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
    read_local,
    record,
    write_base,
)
from setforge.reconcile.wizard import CANCEL
from setforge.reconcile_apply import (
    AutoSide,
    ReconcileAuto,
    ReconcileKind,
    SeedChoice,
    reconcile_plain_file,
)

_PROFILE = "test-apply"


def test_reconcile_auto_members() -> None:
    # ReconcileAuto lives here (co-located with AutoSide) so the CLI --auto
    # surface dispatches on it without importing a legacy marker module.
    assert ReconcileAuto.USE_TRACKED == "use-tracked"
    assert ReconcileAuto.KEEP_LIVE == "keep-live"
    assert {m.value for m in ReconcileAuto} == {"use-tracked", "keep-live"}


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


class TestDeletionOutcomes:
    def test_clean_deletion_is_honored_as_remove(self) -> None:
        fid = _fid()
        record(_PROFILE, fid, base=b"v1\n", local=b"v1\n")
        out = reconcile_plain_file(_PROFILE, fid, live=ABSENT, tracked=b"v1\n")
        assert out.kind is ReconcileKind.REMOVE
        assert out.content is ABSENT
        assert out.new_base == b"v1\n"

    def test_steady_state_absence_is_noop(self) -> None:
        fid = _fid()
        record(_PROFILE, fid, base=b"v1\n", local=ABSENT)
        assert read_local(_PROFILE, fid) is ABSENT
        out = reconcile_plain_file(_PROFILE, fid, live=ABSENT, tracked=b"v1\n")
        assert out.kind is ReconcileKind.NOOP

    def test_delete_modify_defers_not_honored(self) -> None:
        fid = _fid()
        record(_PROFILE, fid, base=b"v1\n", local=b"v1\n")
        out = reconcile_plain_file(_PROFILE, fid, live=ABSENT, tracked=b"v2\n")
        assert out.kind is ReconcileKind.DEFERRED
        assert out.new_base is None


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
        out = reconcile_plain_file(
            _PROFILE, fid, live=b"ours\n", tracked=b"theirs\n", interactive=True
        )
        assert out.kind is ReconcileKind.CANCELLED
        assert out.content is None
        assert out.new_base is None

    def test_non_interactive_conflict_defers_without_prompting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A conflict with no TTY must NOT reach the wizard; it defers.
        fid = self._setup_conflict()

        def _boom(*_a: object, **_k: object) -> object:
            raise AssertionError("resolve_conflicts must not run non-interactively")

        monkeypatch.setattr(reconcile_apply, "resolve_conflicts", _boom)
        out = reconcile_plain_file(_PROFILE, fid, live=b"ours\n", tracked=b"theirs\n")
        assert out.kind is ReconcileKind.DEFERRED
        assert out.new_base is None

    def test_deferred_does_not_rebaseline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fid = self._setup_conflict()
        deferred = WizardResult(MergeResult((Clean(b"ours\n"),)), deferred=True)
        monkeypatch.setattr(
            reconcile_apply, "resolve_conflicts", lambda *a, **k: deferred
        )
        out = reconcile_plain_file(
            _PROFILE, fid, live=b"ours\n", tracked=b"theirs\n", interactive=True
        )
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
        out = reconcile_plain_file(
            _PROFILE, fid, live=b"ours\n", tracked=b"theirs\n", interactive=True
        )
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"merged\n"
        # base advances to tracked (theirs), not to the merged content.
        assert out.new_base == b"theirs\n"


class TestSeed:
    """Divergent pre-existing live file with no recorded base → seed base."""

    def test_non_interactive_keeps_live_and_seeds_base(self) -> None:
        out = reconcile_plain_file(
            _PROFILE, _fid(), live=b"local\n", tracked=b"upstream\n"
        )
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"local\n"  # live kept, never overwritten
        assert out.new_base == b"upstream\n"  # base seeded from upstream
        assert out.seeded is True  # caller warns

    def test_interactive_keep_live(self) -> None:
        out = reconcile_plain_file(
            _PROFILE,
            _fid(),
            live=b"local\n",
            tracked=b"upstream\n",
            interactive=True,
            seed_prompt=lambda _p: SeedChoice.KEEP_LIVE,
        )
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"local\n"
        assert out.new_base == b"upstream\n"
        assert out.seeded is False  # explicit choice, no warn

    def test_interactive_take_upstream_replaces_live(self) -> None:
        out = reconcile_plain_file(
            _PROFILE,
            _fid(),
            live=b"local\n",
            tracked=b"upstream\n",
            interactive=True,
            seed_prompt=lambda _p: SeedChoice.TAKE_UPSTREAM,
        )
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"upstream\n"  # live replaced
        assert out.new_base == b"upstream\n"

    def test_interactive_cancel_aborts(self) -> None:
        out = reconcile_plain_file(
            _PROFILE,
            _fid(),
            live=b"local\n",
            tracked=b"upstream\n",
            interactive=True,
            seed_prompt=lambda _p: CANCEL,
        )
        assert out.kind is ReconcileKind.CANCELLED

    def test_matching_live_no_base_is_not_a_seed(self) -> None:
        # live == tracked with no base: a clean record, not a seed prompt.
        out = reconcile_plain_file(_PROFILE, _fid(), live=b"same\n", tracked=b"same\n")
        assert out.kind is ReconcileKind.WRITE
        assert out.seeded is False
        assert out.new_base == b"same\n"


class TestAuto:
    """Non-interactive --auto resolves conflicts by taking a fixed side."""

    def _conflict_fid(self) -> FileId:
        # base a/b/c; ours edits b->B, theirs edits b->X => conflict on b only.
        fid = _fid()
        write_base(_PROFILE, fid, b"a\nb\nc\n")
        return fid

    def test_auto_ours_keeps_live_per_region(self) -> None:
        fid = self._conflict_fid()
        out = reconcile_plain_file(
            _PROFILE, fid, live=b"a\nB\nc\n", tracked=b"a\nX\nc\n", auto=AutoSide.OURS
        )
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"a\nB\nc\n"  # ours for the conflict, clean lines kept
        assert out.new_base == b"a\nX\nc\n"  # base advances to tracked

    def test_auto_theirs_takes_tracked_per_region(self) -> None:
        fid = self._conflict_fid()
        out = reconcile_plain_file(
            _PROFILE, fid, live=b"a\nB\nc\n", tracked=b"a\nX\nc\n", auto=AutoSide.THEIRS
        )
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"a\nX\nc\n"  # theirs for the conflict
        assert out.new_base == b"a\nX\nc\n"

    def test_auto_resolves_instead_of_deferring(self) -> None:
        # The same conflict without --auto and non-interactively defers.
        fid = self._conflict_fid()
        deferred = reconcile_plain_file(
            _PROFILE, fid, live=b"a\nB\nc\n", tracked=b"a\nX\nc\n"
        )
        assert deferred.kind is ReconcileKind.DEFERRED

    def test_auto_theirs_seeds_from_upstream(self) -> None:
        # No base + divergent live + --auto=use-tracked => take upstream, seed.
        out = reconcile_plain_file(
            _PROFILE, _fid(), live=b"local\n", tracked=b"up\n", auto=AutoSide.THEIRS
        )
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"up\n"
        assert out.new_base == b"up\n"
        assert out.seeded is False  # explicit auto choice, no warn

    def test_auto_ours_seeds_keeping_live(self) -> None:
        out = reconcile_plain_file(
            _PROFILE, _fid(), live=b"local\n", tracked=b"up\n", auto=AutoSide.OURS
        )
        assert out.kind is ReconcileKind.WRITE
        assert out.content == b"local\n"
        assert out.new_base == b"up\n"
        assert out.seeded is False
