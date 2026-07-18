"""Unit/property/security tests for the F7 self-improvement loop (scripts/proposals.py).

Covers, per the F7 acceptance contract: a stable dedup key under volatile
evidence; the append-only ledger's seen-cardinality count + durable
decline-suppress (the regression that task-tracker compaction would have
broken); concurrent appends without lost rows; the emit() 2nd-occurrence rule;
and the safe diff-apply path-confinement.
"""

from __future__ import annotations

import multiprocessing
import re
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts.proposals import (
    Confidence,
    DiffRejected,
    EmitResult,
    Ledger,
    Proposal,
    approve,
    decline,
    emit,
    list_filed,
    mark_applied,
    norm,
    validate_diff_paths,
)


def _p(**kw: object) -> Proposal:
    base: dict[str, object] = dict(
        source="mutmut",
        category="surviving-mutant",
        evidence="mutant survived at merge.py",
        proposed_diff="",
        confidence=Confidence.HIGH,
        file="setforge/reconcile/merge.py",
    )
    return Proposal(**{**base, **kw})  # type: ignore[arg-type]


def _use_tmp_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETFORGE_PROPOSALS_LEDGER", str(tmp_path / "l.jsonl"))


def _mp_append(path_str: str) -> None:
    """Module-level worker so multiprocessing (fork) can import it: append one
    `seen` row from a separate PROCESS — the only way to actually contend the
    cross-process flock (threads are masked by the GIL + O_APPEND atomicity)."""
    Ledger(Path(path_str)).record_seen(
        Proposal(
            source="mutmut",
            category="surviving-mutant",
            evidence="mutant survived at merge.py",
            proposed_diff="",
            confidence=Confidence.HIGH,
            file="setforge/reconcile/merge.py",
        )
    )


# --------------------------------------------------------------------------- #
# Task 1 — schema + stable dedup key
# --------------------------------------------------------------------------- #
def test_evidence_required() -> None:
    with pytest.raises(ValueError, match="evidence"):
        _p(evidence="")


def test_dedup_key_is_16_hex() -> None:
    assert re.fullmatch(r"[0-9a-f]{16}", _p().dedup_key)


def test_norm_strips_volatile_tokens() -> None:
    # Same logical finding, different incidental line/path/timestamp/pid tokens.
    a = _p(evidence="mutant #142 /a/merge.py:88 2026-06-24T10:00:00 pid=9931")
    b = _p(evidence="mutant #999 /b/merge.py:12 2026-06-25T22:31:02 pid=2")
    assert a.dedup_key == b.dedup_key


def test_norm_strips_addresses_uuids_and_epochs() -> None:
    # Hex addresses, UUIDs, and bare epoch/large integers are volatile too.
    a = _p(evidence="leak 0x7f3a a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d 1719230400")
    b = _p(evidence="leak 0xdead f0e1d2c3-b4a5-6978-8a9b-0c1d2e3f4a5b 1700000000")
    assert a.dedup_key == b.dedup_key


@given(st.text())
def test_norm_idempotent(a: str) -> None:
    assert norm(norm(a)) == norm(a)


# --------------------------------------------------------------------------- #
# Task 2 — append-only ledger (count + durable decline-suppress)
# --------------------------------------------------------------------------- #
def test_count_is_seen_cardinality(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.jsonl")
    k = _p().dedup_key
    assert led.count(k) == 0
    led.record_seen(_p())
    led.record_seen(_p())
    assert led.count(k) == 2


def test_decline_is_durable_and_suppresses(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path)
    p = _p()
    led.record_declined(p)
    assert led.is_suppressed(p.dedup_key)
    # A fresh handle (cross-session) still sees the decline — the regression
    # that task-tracker compaction would have broken.
    assert Ledger(path).is_suppressed(p.dedup_key)


def test_concurrent_appends_no_lost_rows(tmp_path: Path) -> None:
    # Real cross-PROCESS contention (threads are masked by the GIL + O_APPEND):
    # 20 processes appending to one ledger must yield 20 rows iff the flock holds.
    path = tmp_path / "ledger.jsonl"
    Ledger(path)  # create the file
    k = _p().dedup_key
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(8) as pool:
        pool.map(_mp_append, [str(path)] * 20)
    assert Ledger(path).count(k) == 20  # flock => no lost append across processes


def test_ledger_tolerates_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path)
    led.record_seen(_p())
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")  # torn / hand-edited garbage
        f.write('{"shape": "wrong"}\n')  # valid JSON, missing key/event
    led.record_seen(_p())
    assert led.count(_p().dedup_key) == 2  # good rows counted, bad lines skipped


# --------------------------------------------------------------------------- #
# Task 4 — emit() orchestration (the 2nd-occurrence rule), ledger-only
# --------------------------------------------------------------------------- #
def test_first_occurrence_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tmp_ledger(tmp_path, monkeypatch)
    assert emit(_p()) is EmitResult.HELD_FIRST_OCCURRENCE
    assert list_filed() == []


def test_second_occurrence_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tmp_ledger(tmp_path, monkeypatch)
    emit(_p(proposed_diff="--- a/x\n+++ b/x\n"))
    assert emit(_p(proposed_diff="--- a/x\n+++ b/x\n")) is EmitResult.FILED
    filed = list_filed()
    assert len(filed) == 1
    assert filed[0].dedup_key == _p().dedup_key
    assert filed[0].proposed_diff == "--- a/x\n+++ b/x\n"


def test_suppressed_key_dropped_even_after_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tmp_ledger(tmp_path, monkeypatch)
    decline(_p())  # human said no (ledger-side; the skill also closes the tracker item)
    assert emit(_p()) is EmitResult.DROPPED_SUPPRESSED
    # A 2nd sighting after the backing tracker item would have been compacted away:
    emit(_p())
    assert emit(_p()) is EmitResult.DROPPED_SUPPRESSED
    assert list_filed() == []


def test_applied_drops_from_filed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_tmp_ledger(tmp_path, monkeypatch)
    emit(_p())
    emit(_p())
    assert len(list_filed()) == 1
    mark_applied(_p())
    assert list_filed() == []


# --------------------------------------------------------------------------- #
# Task 5 — safe diff-apply path-confinement (the security core)
# --------------------------------------------------------------------------- #
def test_rejects_path_traversal() -> None:
    with pytest.raises(DiffRejected):
        validate_diff_paths("--- a/x\n+++ b/../../etc/passwd\n", repo_root="/repo")


def test_rejects_absolute_target() -> None:
    with pytest.raises(DiffRejected):
        validate_diff_paths("--- a/x\n+++ /etc/cron.d/x\n", repo_root="/repo")


def test_rejects_symlink_hunk() -> None:
    with pytest.raises(DiffRejected):
        validate_diff_paths(
            "diff --git a/l b/l\nnew file mode 120000\n+evil\n", repo_root="/repo"
        )


def test_rejects_convert_existing_file_to_symlink() -> None:
    # An existing tracked file flipped to a symlink uses old mode / new mode
    # headers (git apply --check accepts this) — the regex must still reject it.
    with pytest.raises(DiffRejected):
        validate_diff_paths(
            "diff --git a/x b/x\nold mode 100644\nnew mode 120000\n", repo_root="/repo"
        )


def test_accepts_in_repo_diff() -> None:
    # Must not raise.
    validate_diff_paths("--- a/setforge/x.py\n+++ b/setforge/x.py\n", repo_root="/repo")


def test_approve_applies_diff_in_real_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_PROPOSALS_LEDGER", str(tmp_path / "l.jsonl"))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / "hello.txt"
    target.write_text("one\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    diff = "--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-one\n+two\n"
    approve(_p(file="hello.txt", proposed_diff=diff), repo_root=str(tmp_path))
    assert target.read_text() == "two\n"


def test_approve_rejects_unappliable_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_PROPOSALS_LEDGER", str(tmp_path / "l.jsonl"))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    # Context references a file that does not exist → --check fails → DiffRejected.
    diff = "--- a/missing.txt\n+++ b/missing.txt\n@@ -1 +1 @@\n-nope\n+yes\n"
    with pytest.raises(DiffRejected):
        approve(_p(file="missing.txt", proposed_diff=diff), repo_root=str(tmp_path))
