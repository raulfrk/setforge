"""Line-level 3-way merge engine."""

from __future__ import annotations

import importlib

import pytest

from setforge.errors import MergeError, MergeInvariantError
from setforge.reconcile.merge import merge  # the public function
from setforge.reconcile.merge_model import Clean, Conflict, MergeResult
from setforge.reconcile.types import ABSENT

# The façade re-exports `merge` (the function), which shadows the
# `setforge.reconcile.merge` attribute — so fetch the MODULE (for private
# helpers/consts) via importlib rather than `import ... as M`.
M = importlib.import_module("setforge.reconcile.merge")

# --------------------------------------------------------------------------- #
# Task 3 — splitter
# --------------------------------------------------------------------------- #

_CORPUS = [
    b"",
    b"\n",
    b"\r\n",
    b"\r",
    b"a",
    b"abc",
    b"a\r\nb",
    b"a\n\n",
    b"\x00\n",
    b"x\ny\nz",
]


@pytest.mark.parametrize("data", _CORPUS)
def test_split_join_roundtrip(data: bytes) -> None:
    assert b"".join(M._split_lines(data)) == data


def test_split_empty_is_empty_list() -> None:
    assert M._split_lines(b"") == []


# --------------------------------------------------------------------------- #
# Task 4 — absence matrix
# --------------------------------------------------------------------------- #


def _merged(b, o, t):
    return merge(b, o, t).merged()


def test_absence_all_absent() -> None:
    assert merge(ABSENT, ABSENT, ABSENT).merged() is ABSENT


def test_absence_local_add_only() -> None:
    assert _merged(ABSENT, b"new\n", ABSENT) == b"new\n"


def test_absence_upstream_add_only() -> None:
    assert _merged(ABSENT, ABSENT, b"up\n") == b"up\n"


def test_absence_add_add_identical_clean() -> None:
    assert _merged(ABSENT, b"same\n", b"same\n") == b"same\n"


def test_absence_add_add_divergent_conflict() -> None:
    r = merge(ABSENT, b"mine\n", b"theirs\n")
    assert r.clean is False
    (seg,) = r.segments
    assert isinstance(seg, Conflict)
    assert seg.base == b""
    assert seg.ours == b"mine\n"
    assert seg.theirs == b"theirs\n"


def test_absence_local_delete_upstream_unchanged_clean_absent() -> None:
    assert merge(b"x\n", ABSENT, b"x\n").merged() is ABSENT


def test_absence_upstream_delete_local_unchanged_clean_absent() -> None:
    # rev-3 policy: upstream delete + no local edits -> silent delete
    assert merge(b"x\n", b"x\n", ABSENT).merged() is ABSENT


def test_absence_delete_vs_edit_conflict() -> None:
    # ours deleted, theirs edited
    result = merge(b"x\n", ABSENT, b"x2\n")
    assert result.clean is False
    assert result.ours_absent is True


def test_absence_upstream_delete_with_local_edits_conflict() -> None:
    # rev-3 policy: upstream delete + local edits -> conflict (you'd lose edits)
    r = merge(b"x\n", b"mine\n", ABSENT)
    assert r.clean is False
    (seg,) = r.segments
    assert isinstance(seg, Conflict)
    assert seg.theirs == b""  # absent side rendered as b""
    assert r.theirs_absent is True


def test_absence_both_delete_clean_absent() -> None:
    assert merge(b"x\n", ABSENT, ABSENT).merged() is ABSENT


# --------------------------------------------------------------------------- #
# Task 5 — body merge + degrade
# --------------------------------------------------------------------------- #


def test_body_clean_upstream_change_applies() -> None:
    assert _merged(b"a\nb\nc\n", b"a\nb\nc\n", b"a\nB\nc\n") == b"a\nB\nc\n"


def test_body_conflict_overlapping() -> None:
    r = merge(b"a\nb\nc\n", b"a\nMINE\nc\n", b"a\nUP\nc\n")
    assert r.clean is False
    assert any(isinstance(s, Conflict) for s in r.segments)


def test_binary_nul_whole_file_conflict() -> None:
    r = merge(b"a\x00b", b"a\x00B", b"a\x00C")
    assert r.clean is False
    (seg,) = r.segments
    assert isinstance(seg, Conflict)


def test_oversize_whole_file_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(M, "_MAX_BYTES", 8)
    r = merge(b"a\nb\nc\nd\ne\n", b"a\nX\nc\nd\ne\n", b"a\nb\nc\nd\nY\n")
    assert r.clean is False
    assert len(r.segments) == 1
    assert isinstance(r.segments[0], Conflict)


# --------------------------------------------------------------------------- #
# Task 6 — identities + verify
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("x", [b"a\nb\nc\n", b"a\r\nb", b"", b"no-newline", b"\n\n"])
def test_self_merge_identity(x: bytes) -> None:
    assert merge(x, x, x).merged() == x


def test_one_sided_theirs() -> None:
    assert _merged(b"a\nb\n", b"a\nb\n", b"a\nB\n") == b"a\nB\n"


def test_one_sided_ours() -> None:
    assert _merged(b"a\nb\n", b"a\nB\n", b"a\nb\n") == b"a\nB\n"


def test_same_change_both_sides_clean() -> None:
    assert _merged(b"red\n", b"blue\n", b"blue\n") == b"blue\n"


def test_final_newline_change_not_dropped() -> None:
    # theirs drops the final newline, ours unchanged -> theirs wins, not silently kept
    assert _merged(b"a\n", b"a\n", b"a") == b"a"


def test_verify_catches_dropped_user_edit() -> None:
    # a result that silently drops a user edit (a line ours has beyond base) must
    # be rejected by _verify (INV-1 edit-preservation guard).
    base, ours, theirs = b"a\n", b"a\nEDIT\n", b"a\n"
    M._verify(base, ours, theirs, MergeResult((Clean(b"a\nEDIT\n"),)))  # no raise
    dropped = MergeResult((Clean(b"a\n"),))  # EDIT silently gone
    with pytest.raises(MergeInvariantError):
        M._verify(base, ours, theirs, dropped)


# --------------------------------------------------------------------------- #
# Degrade paths + matcher wiring (Phase-5 review additions)
# --------------------------------------------------------------------------- #


class _BoomGroups:
    """A fake Merge3 whose merge_groups() raises the configured exception."""

    exc: type[BaseException] = RecursionError

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def merge_groups(self) -> list[object]:
        raise self.exc


def test_recursion_error_degrades_to_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(M.merge3, "Merge3", _BoomGroups)
    r = merge(b"a\nb\n", b"a\nX\n", b"a\nY\n")
    assert r.clean is False
    assert isinstance(r.segments[0], Conflict)


def test_max_recursion_depth_degrades_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the pure-Python matcher's MaxRecursionDepth (an Exception subclass) must
    # also degrade, not be wrapped as MergeError (totality on pure-Python builds).
    class _Boom(_BoomGroups):
        exc = M._MaxRecursionDepth

    monkeypatch.setattr(M.merge3, "Merge3", _Boom)
    assert merge(b"a\nb\n", b"a\nX\n", b"a\nY\n").clean is False


def test_generic_merge3_error_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom(_BoomGroups):
        exc = ValueError

    monkeypatch.setattr(M.merge3, "Merge3", _Boom)
    with pytest.raises(MergeError, match="merge3 failed"):
        merge(b"a\nb\n", b"a\nX\n", b"a\nY\n")


def test_max_lines_degrades_to_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(M, "_MAX_LINES", 2)
    r = merge(b"a\nb\nc\nd\n", b"a\nX\nc\nd\n", b"a\nb\nc\nY\n")
    assert r.clean is False
    assert len(r.segments) == 1
    assert isinstance(r.segments[0], Conflict)


def test_merge_uses_patience_matcher(monkeypatch: pytest.MonkeyPatch) -> None:
    # directly kill the "swapped to difflib default" mutant: assert merge passes
    # PatienceSequenceMatcher into Merge3.
    captured: dict[str, object] = {}
    real = M.merge3.Merge3

    def _spy(*args: object, **kwargs: object) -> object:
        captured["sm"] = kwargs.get("sequence_matcher")
        return real(*args, **kwargs)

    monkeypatch.setattr(M.merge3, "Merge3", _spy)
    merge(b"a\nb\n", b"a\nX\n", b"a\nb\n")
    assert captured["sm"] is M.PatienceSequenceMatcher


def test_conflict_keeps_flanking_clean_regions_byte_exact() -> None:
    # INV-6: the non-conflict lines around a conflict stay byte-identical.
    r = merge(b"a\nb\nc\n", b"a\nMINE\nc\n", b"a\nUP\nc\n")
    assert r.clean is False
    assert r.segments[0] == Clean(b"a\n")
    assert r.segments[-1] == Clean(b"c\n")
