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

import pytest

from setforge.cli import _install_helpers
from setforge.cli._helpers import ProfileContext, _resolve_drift_paths
from setforge.compare import CompareReport, CompareStatus, DriftClass, FileCompare
from setforge.config import Config, Profile, ResolvedProfile, TrackedFile

_LIVE_WITH_MARKERS = (
    "intro\n"
    "<!-- setforge:user-section start shared R -->\n"
    "body\n"
    "<!-- setforge:user-section end shared R -->\n"
    "outro\n"
)
_STRIPPED = "intro\nbody\noutro\n"


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
        auto_accept_tracked=False,
        auto_accept_live=False,
    )


def test_dry_run_drift_gate_counts_diff_only_unexpected_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A diff-only ``UNEXPECTED`` entry counts toward the dry-run gate line.

    The dry-run count keys off the compare-level classification
    (``drift_class``), not ``mode_drift`` — so a DRIFTED entry with
    ``mode_drift=False`` still renders ``unexpected drift in 1 file(s)``
    even though the live install gate (:func:`_check_unexpected_drift`)
    would not reject it. Pins the wider-than-the-live-gate semantics the
    helper's docstring documents.
    """
    report = CompareReport(
        entries=[
            FileCompare(
                name="claude/CLAUDE.md",
                status=CompareStatus.DRIFTED,
                diff="--- a\n+++ b\n",
                mode_drift=False,
                drift_class=DriftClass.UNEXPECTED,
            ),
        ],
        has_unexpected_drift=True,
    )
    _install_helpers._dry_run_emit_drift_gate(report)
    out = capsys.readouterr().out
    assert "unexpected drift in 1 file(s)" in out


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
            ),
            FileCompare(
                name=name2,
                status=CompareStatus.DRIFTED,
                diff="--- a\n+++ b\n",
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
