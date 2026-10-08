"""Smoke tests for :mod:`setforge.cli._install_helpers`.

The heavy lifting is covered by ``tests/test_install.py`` plus the
Docker e2e suite. These tests exist so a future structural rename of
the helper surface fails fast (import-error class) and so the
no-drift short-circuit on :func:`_run_predeploy_gates` is anchored
explicitly.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import typer

from setforge.cli import _install_helpers
from setforge.cli._helpers import ProfileContext, _resolve_drift_paths
from setforge.compare import CompareReport, CompareStatus, DriftClass, FileCompare
from setforge.config import Config, Profile, ResolvedProfile, TrackedFile


def test_install_helpers_module_imports() -> None:
    """The public-to-install helpers are exported and callable."""
    assert callable(_install_helpers._run_predeploy_gates)
    assert callable(_install_helpers._write_install_transition)


def test_claude_merge_factory_loads_only_for_interactive_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.reconcile import claude_merge
    from setforge.reconcile.conflict_choices import claude_merge_unavailable

    sentinel = lambda _conflict: b"merged"  # noqa: E731
    monkeypatch.setattr(claude_merge, "make_claude_merge_fn", lambda **_kw: sentinel)

    assert (
        _install_helpers._claude_merge_for(Path("live"), interactive=False)
        is claude_merge_unavailable
    )
    assert (
        _install_helpers._claude_merge_for(Path("live"), interactive=True) is sentinel
    )


def test_run_predeploy_gates_no_entries_is_noop(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty :class:`CompareReport` → short-circuit that returns ``None`` and
    emits NOTHING.

    The short-circuit's only observable side effect on the reject path is
    the ``typer.secho`` error write that precedes ``raise typer.Exit(1)``.
    This pins the no-drift contract on both axes: it must not raise / Exit,
    AND it must not emit anything. ``typer.secho`` is monkeypatched to a
    sentinel that fails the test if it fires (catches a mutation that moves
    the error write above the guard), and ``capsys`` asserts stdout/stderr
    stay empty (catches any other stray write). ``ProfileContext`` is
    unreachable on this short-circuit path so the test passes ``None``
    deliberately — the cast keeps mypy honest about the deliberate
    violation that the short-circuit contract permits.
    """

    def _fail_on_secho(*_args: object, **_kwargs: object) -> None:
        pytest.fail("_run_predeploy_gates wrote output on the no-drift path")

    monkeypatch.setattr(_install_helpers.typer, "secho", _fail_on_secho)

    empty = CompareReport(entries=[], has_unexpected_drift=False)
    _install_helpers._run_predeploy_gates(
        drift_report=empty, ctx=cast(ProfileContext, None), yes=False
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(("mode_drift", "gated"), [(False, 0), (True, 1)])
def test_dry_run_drift_gate_counts_what_the_install_gate_rejects(
    capsys: pytest.CaptureFixture[str], mode_drift: bool, gated: int
) -> None:
    """The dry-run gate line counts exactly the files a real install refuses.

    The install gate (:func:`_run_predeploy_gates`) trips only on
    permission-mode drift, so content-only drift — which install reconciles
    without a gate — must not be counted by the preview.
    """
    report = CompareReport(
        entries=[
            FileCompare(
                name="claude/CLAUDE.md",
                status=CompareStatus.DRIFTED,
                diff="--- a\n+++ b\n",
                mode_drift=mode_drift,
                drift_class=DriftClass.UNEXPECTED,
            ),
        ],
        has_unexpected_drift=True,
    )
    ctx = cast(ProfileContext, SimpleNamespace(profile="p"))

    _install_helpers._dry_run_emit_drift_gate(report)
    assert f"unexpected drift in {gated} file(s)" in capsys.readouterr().out

    if gated:
        with pytest.raises(typer.Exit):
            _install_helpers._run_predeploy_gates(
                drift_report=report, ctx=ctx, yes=False
            )
    else:
        _install_helpers._run_predeploy_gates(drift_report=report, ctx=ctx, yes=False)


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
