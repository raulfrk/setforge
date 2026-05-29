"""Smoke tests for :mod:`setforge.cli._install_helpers`.

The heavy lifting is covered by ``tests/test_install.py`` plus the
Docker e2e suite. These tests exist so a future structural rename of
the helper surface fails fast (import-error class) and so the
no-drift short-circuit on :func:`_check_unexpected_drift` is anchored
explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from setforge.cli import _install_helpers
from setforge.cli._helpers import ProfileContext, _resolve_drift_paths
from setforge.compare import CompareReport, CompareStatus, FileCompare
from setforge.config import Config, Profile, ResolvedProfile, TrackedFile


def test_install_helpers_module_imports() -> None:
    """The three public-to-install helpers are exported and callable."""
    assert callable(_install_helpers._check_unexpected_drift)
    assert callable(_install_helpers._deploy_all_tracked_files)
    assert callable(_install_helpers._write_install_transition)


def test_check_unexpected_drift_no_entries_is_noop() -> None:
    """Empty :class:`CompareReport` → short-circuit, no side effect, no Exit.

    The helper returns ``None`` unconditionally; the assertion is that the
    no-drift call doesn't raise / Exit. ``ProfileContext`` is unreachable
    on this short-circuit path so the test passes ``None`` deliberately —
    the cast keeps mypy honest about the deliberate violation that the
    short-circuit contract permits.
    """
    empty = CompareReport(entries=[], has_unexpected_drift=False)
    _install_helpers._check_unexpected_drift(
        empty,
        cast(ProfileContext, None),
        Path("/tmp/setforge.yaml"),
        auto_accept_tracked=False,
        auto_accept_live=False,
    )


def test_resolve_drift_paths_directory_subfiles_do_not_collide(
    tmp_path: Path,
) -> None:
    """Two sub-files sharing a basename resolve to distinct paths.

    A directory tracked_file with ``sub1/x.txt`` and ``sub2/x.txt``
    expands to two synthetic names (``mydir/sub1/x.txt`` and
    ``mydir/sub2/x.txt``). Keying by the synthetic name keeps both
    entries; the earlier sub-file must resolve to ITS own paths, not be
    overwritten by the later same-basename sibling.
    """
    repo_root = tmp_path / "repo"
    tracked_root = repo_root / "tracked" / "mydir"
    (tracked_root / "sub1").mkdir(parents=True)
    (tracked_root / "sub2").mkdir(parents=True)
    (tracked_root / "sub1" / "x.txt").write_text("one\n", encoding="utf-8")
    (tracked_root / "sub2" / "x.txt").write_text("two\n", encoding="utf-8")
    dst_root = tmp_path / "live"

    tracked_file = TrackedFile(src=Path("mydir"), dst=str(dst_root))
    cfg = Config(
        tracked_files={"mydir": tracked_file},
        profiles={"p": Profile(tracked_files=["mydir"])},
    )
    resolved = ResolvedProfile(tracked_files=["mydir"])
    ctx = ProfileContext(cfg=cfg, resolved=resolved, repo_root=repo_root, profile="p")

    name1 = "mydir/sub1/x.txt"
    name2 = "mydir/sub2/x.txt"
    report = CompareReport(
        entries=[
            FileCompare(
                name=name1,
                status=CompareStatus.DRIFTED,
                diff="--- a\n+++ b\n",
                expected_drift_keys=[],
                unexpected_drift_keys=[],
            ),
            FileCompare(
                name=name2,
                status=CompareStatus.DRIFTED,
                diff="--- a\n+++ b\n",
                expected_drift_keys=[],
                unexpected_drift_keys=[],
            ),
        ],
        has_unexpected_drift=True,
    )

    resolved_entries = _resolve_drift_paths(report, ctx)
    by_name = {
        entry.name: (sub_src, sub_dst) for entry, sub_src, sub_dst in resolved_entries
    }

    assert by_name[name1][0] == tracked_root / "sub1" / "x.txt"
    assert by_name[name2][0] == tracked_root / "sub2" / "x.txt"
    # The earlier sub-file did NOT collapse onto the later one.
    assert by_name[name1][0] != by_name[name2][0]
    assert by_name[name1][1] != by_name[name2][1]
