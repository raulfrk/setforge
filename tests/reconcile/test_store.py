"""The reconcile store: resolver, local/base, index, reconstruct, verify."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from setforge.errors import (
    CorruptIndexError,
    InvariantViolation,
    ReconcileStoreError,
    UnsafeFileId,
)
from setforge.reconcile import store
from setforge.reconcile.types import ABSENT, Absent, HunkClass, file_id

# --------------------------------------------------------------------------- #
# Resolver (Task 4)
# --------------------------------------------------------------------------- #


def test_resolver_ok(tmp_state: Path) -> None:
    p = store._resolve(store._local_root(), "deb", file_id("claude/CLAUDE.md"))
    assert str(p).endswith("/local/deb/claude/CLAUDE.md")


@pytest.mark.parametrize("profile", ["../../etc", "..", "", ".", "a/b", "x\x00y"])
def test_resolver_rejects_bad_profile(tmp_state: Path, profile: str) -> None:
    with pytest.raises(UnsafeFileId):
        store._resolve(store._local_root(), profile, file_id("x"))


@pytest.mark.parametrize("fid", ["a/../../x", "/abs", "..", "", "a/\x00/b"])
def test_resolver_rejects_bad_fileid(tmp_state: Path, fid: str) -> None:
    with pytest.raises(UnsafeFileId):
        store._resolve(store._local_root(), "p", fid)  # raw str; resolver re-checks


def test_index_path_rejects_bad_profile(tmp_state: Path) -> None:
    with pytest.raises(UnsafeFileId):
        store._index_path("../x")


# --------------------------------------------------------------------------- #
# Local store + base passthrough (Task 5)
# --------------------------------------------------------------------------- #


def test_local_trichotomy(tmp_state: Path) -> None:
    fid = file_id("f")
    assert store.read_local("p", fid) is None  # not recorded
    store.write_local("p", fid, b"")
    assert store.read_local("p", fid) == b""  # legitimately empty
    store.write_local("p", fid, ABSENT)
    assert store.read_local("p", fid) is ABSENT  # explicit absence


def test_local_bytes_verbatim(tmp_state: Path) -> None:
    fid = file_id("f")
    data = b"a\r\nb\x00c"  # CRLF + NUL, no trailing newline
    store.write_local("p", fid, data)
    assert store.read_local("p", fid) == data


def test_local_perms(tmp_state: Path) -> None:
    fid = file_id("f")
    store.write_local("p", fid, b"x")
    path = store._resolve(store._local_root(), "p", fid)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o077 == 0


def test_absent_then_content_clears_marker(tmp_state: Path) -> None:
    fid = file_id("f")
    store.write_local("p", fid, ABSENT)
    store.write_local("p", fid, b"now here")
    assert store.read_local("p", fid) == b"now here"


# --------------------------------------------------------------------------- #
# drafts/ store (A5c)
# --------------------------------------------------------------------------- #


def test_drafts_round_trip(tmp_state: Path) -> None:
    fid = file_id("claude/CLAUDE.md")
    drafts = {"sha256:aa": b"Land them in ~/projects/wt/.\n", "sha256:bb": b"other"}
    store.write_drafts("p", fid, drafts)
    assert store.read_drafts("p", fid) == drafts


def test_drafts_verbatim_bytes(tmp_state: Path) -> None:
    fid = file_id("f")
    data = (
        b"a\r\nb\x00c"  # CRLF + NUL, no trailing newline — survives base64 round-trip
    )
    store.write_drafts("p", fid, {"sha256:aa": data})
    assert store.read_drafts("p", fid)["sha256:aa"] == data


def test_drafts_absent_is_empty(tmp_state: Path) -> None:
    assert store.read_drafts("p", file_id("f")) == {}


def test_drafts_empty_removes_manifest(tmp_state: Path) -> None:
    fid = file_id("f")
    store.write_drafts("p", fid, {"sha256:aa": b"x"})
    store.write_drafts("p", fid, {})  # demote: no drafted hunks remain
    assert store.read_drafts("p", fid) == {}
    assert not store._drafts_path("p", fid).exists()


def test_drafts_perms(tmp_state: Path) -> None:
    fid = file_id("f")
    store.write_drafts("p", fid, {"sha256:aa": b"x"})
    path = store._drafts_path("p", fid)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o077 == 0


def test_drafts_corrupt_manifest_raises(tmp_state: Path) -> None:
    fid = file_id("f")
    store.write_drafts("p", fid, {"sha256:aa": b"x"})
    store._drafts_path("p", fid).write_text("{not json", encoding="utf-8")
    with pytest.raises(ReconcileStoreError):
        store.read_drafts("p", fid)


@pytest.mark.parametrize("bad_value", ["123", "null", "true"])
def test_drafts_non_string_value_raises(tmp_state: Path, bad_value: str) -> None:
    # a non-string scalar manifest value must fail closed, not str()-coerce into
    # garbage bytes (a number/null/bool would otherwise base64-decode silently).
    fid = file_id("f")
    store.write_drafts("p", fid, {"sha256:aa": b"x"})
    store._drafts_path("p", fid).write_text(
        f'{{"sha256:aa": {bad_value}}}', encoding="utf-8"
    )
    with pytest.raises(ReconcileStoreError):
        store.read_drafts("p", fid)


def _drafted_row(anchor: str, draft: bytes) -> dict[str, object]:
    return {
        "cls": HunkClass.SHARED_DRAFTED.value,
        "label": "## Worktrees",
        "live_hash": "sha256:live",
        "anchor": anchor,
        "draft_hash": store._sha(draft),
    }


def test_record_writes_drafts_and_verifies(tmp_state: Path) -> None:
    fid = file_id("f")
    draft = b"Land them in ~/projects/wt/.\n"
    store.record(
        "p",
        fid,
        base=b"base\n",
        local=b"live\n",
        hunks=[_drafted_row("sha256:aa", draft)],
        drafts={"sha256:aa": draft},
    )
    assert store.read_drafts("p", fid) == {"sha256:aa": draft}
    store.verify("p", fid)  # no raise: manifest matches the SHARED_DRAFTED hunk


def test_verify_orphan_draft_raises(tmp_state: Path) -> None:
    # a drafts manifest with an anchor that no SHARED_DRAFTED hunk claims.
    fid = file_id("f")
    store.record("p", fid, base=b"b", local=b"l", hunks=[], drafts={"sha256:aa": b"x"})
    with pytest.raises(InvariantViolation, match="INV-10"):
        store.verify("p", fid)


def test_verify_missing_draft_for_drafted_row_raises(tmp_state: Path) -> None:
    # an index SHARED_DRAFTED row with no draft blob (the torn-restore failure).
    fid = file_id("f")
    store.record(
        "p",
        fid,
        base=b"b",
        local=b"l",
        hunks=[_drafted_row("sha256:aa", b"x")],
        drafts={},
    )
    with pytest.raises(InvariantViolation, match="INV-10"):
        store.verify("p", fid)


def test_verify_draft_hash_mismatch_raises(tmp_state: Path) -> None:
    fid = file_id("f")
    row = _drafted_row("sha256:aa", b"correct")
    store.record(
        "p", fid, base=b"b", local=b"l", hunks=[row], drafts={"sha256:aa": b"TAMPERED"}
    )
    with pytest.raises(InvariantViolation, match="INV-2"):
        store.verify("p", fid)


def test_base_passthrough(tmp_state: Path) -> None:
    fid = file_id("c/d.md")
    assert store.read_base("p", fid) is None
    store.write_base("p", fid, b"base bytes")
    assert store.read_base("p", fid) == b"base bytes"


# --------------------------------------------------------------------------- #
# Index read/write (Task 6)
# --------------------------------------------------------------------------- #


def test_read_index_absent_is_empty(tmp_state: Path) -> None:
    assert store.read_index("p").files == {}


def test_index_round_trip(tmp_state: Path) -> None:
    from setforge.reconcile.index_model import FileEntry, Index

    idx = Index(files={"f": FileEntry(present=True, local_hash="sha256:00", hunks=[])})
    store.write_index("p", idx)
    assert store.read_index("p") == idx


def test_index_file_perms(tmp_state: Path) -> None:
    from setforge.reconcile.index_model import Index

    store.write_index("p", Index(files={}))
    assert store._index_path("p").stat().st_mode & 0o777 == 0o600


def test_read_index_corrupt_raises(tmp_state: Path) -> None:
    store.write_index(
        "p",
        __import__("setforge.reconcile.index_model", fromlist=["Index"]).Index(
            files={}
        ),
    )
    store._index_path("p").write_text("{garbage", encoding="utf-8")
    with pytest.raises(CorruptIndexError):
        store.read_index("p")


# --------------------------------------------------------------------------- #
# reconstruct + verify (Task 7)
# --------------------------------------------------------------------------- #


def _record_locked(
    profile: str,
    fid,
    *,
    base: bytes,
    local: bytes | Absent,
    hunks: list[dict[str, object]] | None = None,
) -> None:
    from setforge import locking

    with locking.profile_lock(profile):
        store.record(profile, fid, base=base, local=local, hunks=hunks)


def test_reconstruct_is_local(tmp_state: Path) -> None:
    fid = file_id("f")
    store.write_local("p", fid, b"hi")
    assert store.reconstruct("p", fid) == b"hi"


def test_verify_ok_after_record(tmp_state: Path) -> None:
    fid = file_id("f")
    _record_locked("p", fid, base=b"B", local=b"L")
    store.verify("p", fid)  # no raise
    entry = store.read_index("p").files["f"]
    assert entry.local_hash == "sha256:" + hashlib.sha256(b"L").hexdigest()


def test_verify_detects_missing_local(tmp_state: Path) -> None:
    fid = file_id("f")
    _record_locked("p", fid, base=b"B", local=b"L")
    store._resolve(store._local_root(), "p", fid).unlink()
    with pytest.raises(InvariantViolation):
        store.verify("p", fid)


def test_verify_detects_orphan_local(tmp_state: Path) -> None:
    # a local file with no index entry is an INV-10 orphan
    fid = file_id("f")
    store.write_local("p", fid, b"L")  # local written, no index entry
    with pytest.raises(InvariantViolation):
        store.verify("p", fid)


def test_verify_detects_hash_mismatch(tmp_state: Path) -> None:
    fid = file_id("f")
    _record_locked("p", fid, base=b"B", local=b"L")
    store._resolve(store._local_root(), "p", fid).write_bytes(b"tampered")
    with pytest.raises(InvariantViolation):
        store.verify("p", fid)


def test_verify_ok_after_record_absent(tmp_state: Path) -> None:
    # the ABSENT-recorded leg: present=False, local_hash=None, marker on disk.
    fid = file_id("f")
    _record_locked("p", fid, base=b"B", local=ABSENT)
    store.verify("p", fid)  # no raise
    entry = store.read_index("p").files["f"]
    assert entry.present is False
    assert entry.local_hash is None


def test_verify_ok_after_record_empty_bytes(tmp_state: Path) -> None:
    # b"" is a present, recorded file — distinct from ABSENT.
    fid = file_id("f")
    _record_locked("p", fid, base=b"B", local=b"")
    store.verify("p", fid)
    assert store.read_index("p").files["f"].present is True


def test_verify_detects_present_flag_disagreement(tmp_state: Path) -> None:
    # index says present, but on-disk state is the absence marker -> INV-10.
    from setforge.reconcile.index_model import FileEntry, Index

    fid = file_id("f")
    _record_locked("p", fid, base=b"B", local=ABSENT)  # writes present=False
    # rewrite the index to claim present=True while the marker still says absent:
    store.write_index(
        "p",
        Index(files={"f": FileEntry(present=True, local_hash="sha256:00", hunks=[])}),
    )
    with pytest.raises(InvariantViolation):
        store.verify("p", fid)


def test_verify_whole_index_discovers_orphan(tmp_state: Path) -> None:
    # verify(profile) with no fid must DISCOVER an on-disk orphan it was not told
    # about (INV-10, disk->index direction via the full scan).
    keep = file_id("keep")
    _record_locked("p", keep, base=b"B", local=b"L")
    # plant an on-disk local file with no index entry:
    store.write_local("p", file_id("orphan"), b"X")
    with pytest.raises(InvariantViolation):
        store.verify("p")  # fid=None -> whole-index + on-disk union scan


def test_verify_whole_index_passes_when_consistent(tmp_state: Path) -> None:
    _record_locked("p", file_id("a"), base=b"B1", local=b"L1")
    _record_locked("p", file_id("b"), base=b"B2", local=ABSENT)
    store.verify("p")  # no raise over the whole profile


# --------------------------------------------------------------------------- #
# record (index-last) + prune + lock contract (Task 8)
# --------------------------------------------------------------------------- #


def test_record_writes_index_last(
    tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(store, "write_base", lambda *a, **k: calls.append("base"))
    monkeypatch.setattr(store, "write_local", lambda *a, **k: calls.append("local"))
    monkeypatch.setattr(store, "write_index", lambda *a, **k: calls.append("index"))
    monkeypatch.setattr(
        store,
        "read_index",
        lambda p: __import__(
            "setforge.reconcile.index_model", fromlist=["Index"]
        ).Index(files={}),
    )
    store.record("p", file_id("f"), base=b"B", local=b"L")
    assert calls == ["base", "local", "index"]


def test_record_and_prune_do_not_take_lock(
    tmp_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A1 must NOT acquire profile_lock itself — the caller holds it; a second
    # in-process acquisition would deadlock.
    from setforge import locking

    taken: list[str] = []
    monkeypatch.setattr(locking, "profile_lock", lambda *a, **k: taken.append("lock"))
    store.record("p", file_id("f"), base=b"B", local=b"L")
    store.prune("p", {file_id("f")})
    assert taken == []


_HUNK: dict[str, object] = {
    "cls": "shared",
    "label": "## Shell",
    "live_hash": "sha256:aa",
    "anchor": "sha256:bb",
}


def test_record_with_no_hunks_preserves_existing(tmp_state: Path) -> None:
    # A5 staging records a classification; a later install writeback through
    # record() (hunks=None) must NOT flatten it away.
    fid = file_id("f")
    _record_locked("p", fid, base=b"B", local=b"L", hunks=[_HUNK])
    assert store.read_index("p").files["f"].hunks == [_HUNK]
    _record_locked("p", fid, base=b"B2", local=b"L2")  # install-style: no hunks
    assert store.read_index("p").files["f"].hunks == [_HUNK]  # preserved


def test_record_with_explicit_hunks_overwrites(tmp_state: Path) -> None:
    fid = file_id("f")
    _record_locked("p", fid, base=b"B", local=b"L", hunks=[_HUNK])
    other = {**_HUNK, "cls": "local"}
    _record_locked("p", fid, base=b"B", local=b"L", hunks=[other])
    assert store.read_index("p").files["f"].hunks == [other]


def test_record_first_time_seeds_empty_hunks(tmp_state: Path) -> None:
    _record_locked("p", file_id("f"), base=b"B", local=b"L")  # no prior entry
    assert store.read_index("p").files["f"].hunks == []


def test_prune_removes_unlisted_keeps_listed(tmp_state: Path) -> None:
    keep, drop = file_id("keep"), file_id("drop")
    _record_locked("p", keep, base=b"B1", local=b"L1")
    _record_locked("p", drop, base=b"B2", local=b"L2")
    store.prune("p", {keep})
    assert store.read_local("p", keep) == b"L1"
    assert store.read_local("p", drop) is None
    assert "drop" not in store.read_index("p").files
    assert "keep" in store.read_index("p").files


def test_prune_removes_absence_marker(tmp_state: Path) -> None:
    # an ABSENT-recorded file's marker (in the separate local-absent/ tree) is
    # pruned too when its file-id drops out of the live set.
    keep, drop = file_id("keep"), file_id("gone")
    _record_locked("p", keep, base=b"B1", local=b"L1")
    _record_locked("p", drop, base=b"B2", local=ABSENT)
    assert store.read_local("p", drop) is ABSENT
    store.prune("p", {keep})
    assert store.read_local("p", drop) is None  # marker gone
    assert "gone" not in store.read_index("p").files
    store.verify("p")  # store stays consistent after prune
