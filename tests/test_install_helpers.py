"""Smoke tests for :mod:`setforge.cli._install_helpers`.

The heavy lifting is covered by ``tests/test_install.py`` plus the
Docker e2e suite. These tests exist so a future structural rename of
the helper surface fails fast (import-error class) and so the
no-drift short-circuit on :func:`_check_unexpected_drift` is anchored
explicitly.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import cast

import pytest

from setforge import base_store, spans_store
from setforge.cli import _install_helpers
from setforge.cli._helpers import ProfileContext, _resolve_drift_paths
from setforge.compare import CompareReport, CompareStatus, DriftClass, FileCompare
from setforge.config import Config, Profile, ResolvedProfile, TrackedFile
from setforge.source import AnchorAtEndOfFile, HostLocalSection, HostLocalSectionName
from setforge.spans import OverlaySpanPayload, SpanEntry, SpanKind

_LIVE_WITH_MARKERS = (
    "intro\n"
    "<!-- setforge:user-section start shared R -->\n"
    "body\n"
    "<!-- setforge:user-section end shared R -->\n"
    "outro\n"
)
_STRIPPED = "intro\nbody\noutro\n"

_HL_HASH = "a" * 64  # any well-formed sha256-hex parses; rewritten before strip.
_PLACEHOLDER_PY = (
    "<!-- setforge:user-section start host-local python -->\n"
    f"<!-- setforge:user-section end host-local python hash={_HL_HASH} -->\n"
)


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
    _install_helpers._dry_run_emit_drift_gate(report, live_sections_map={})
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


def test_migration_plan_is_pure_and_apply_seeds_base_before_stripping_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planning writes nothing; the apply seeds the base BEFORE stripping live.

    The plan/apply split is the refuse-before-write seam: the plan must leave
    the filesystem untouched, and the apply's seed-first ordering is the
    crash-safety invariant — a kill between the two steps must leave
    base-present + live-marker-bearing (resumable), never
    base-absent-after-strip. Records the interleaving of ``write_base`` and
    the live rewrite and asserts the base write lands first.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "note.md"
    live.write_text(_LIVE_WITH_MARKERS, encoding="utf-8")

    order: list[str] = []
    real_write_base = base_store.write_base
    real_rewrite = _install_helpers._atomic_rewrite_preserving_mode

    def _record_write_base(profile: str, file_id: str, data: bytes) -> None:
        order.append("base")
        real_write_base(profile, file_id, data)

    def _record_rewrite(path: Path, content: str, mode: int) -> None:
        order.append("live")
        real_rewrite(path, content, mode)

    monkeypatch.setattr(
        "setforge.cli._install_helpers.base_store.write_base", _record_write_base
    )
    monkeypatch.setattr(
        _install_helpers, "_atomic_rewrite_preserving_mode", _record_rewrite
    )

    plan = _install_helpers._plan_disposition_base("p", "f", live)

    # The plan is a pure read: nothing written, live untouched.
    assert plan.migrated is True
    assert plan.base_text == _STRIPPED
    assert plan.deferred_seed == _STRIPPED.encode("utf-8")
    assert plan.deferred_live_strip == _STRIPPED
    assert order == []
    assert base_store.read_base("p", "f") is None
    assert live.read_text(encoding="utf-8") == _LIVE_WITH_MARKERS

    _install_helpers._apply_deferred_base_migration("p", "f", live, plan)

    # base seeded FIRST, live stripped SECOND.
    assert order == ["base", "live"]
    assert base_store.read_base("p", "f") == _STRIPPED.encode("utf-8")
    assert live.read_text(encoding="utf-8") == _STRIPPED


def test_resume_marker_strip_completes_without_reseeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash-resume: a base-present, live-marker-bearing file finishes the strip.

    Reproduces the crash-resume state (base seeded, live still marker-bearing)
    and drives the plan + apply pair: the plan defers a live rewrite to the
    stripped form matching the already-seeded base WITHOUT a
    ``deferred_seed``, and the apply never calls ``write_base``.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "note.md"
    live.write_text(_LIVE_WITH_MARKERS, encoding="utf-8")
    live.chmod(0o600)
    base_store.write_base("p", "f", _STRIPPED.encode("utf-8"))

    reseeded: list[bytes] = []
    monkeypatch.setattr(
        "setforge.cli._install_helpers.base_store.write_base",
        lambda profile, file_id, data: reseeded.append(data),
    )

    plan = _install_helpers._plan_disposition_base("p", "f", live)
    assert plan.migrated is False  # the seed ran on a prior install
    assert plan.deferred_seed is None
    assert plan.deferred_live_strip == _STRIPPED

    _install_helpers._apply_deferred_base_migration("p", "f", live, plan)

    # Strip landed; mode preserved; NO re-seed.
    assert live.read_text(encoding="utf-8") == _STRIPPED
    assert stat.S_IMODE(live.stat().st_mode) == 0o600
    assert reseeded == []


def test_deploy_preserve_overlay_loads_spans_and_advances_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disposition=None file with host-local OVERLAY spans: markerless + sidecar.

    Regression for the un-gated spans load: before the fix ``file_spans`` was
    populated ONLY when ``disposition is not None``, so a disposition=None file
    carrying host-local overlay spans deployed WITH markers and never advanced its
    spans sidecar. The gate now keys on ``tracked_file.spans``.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo_root = tmp_path / "repo"
    tracked_root = repo_root / "tracked"
    tracked_root.mkdir(parents=True)
    body = "## Python\n\nuse uv\n"
    (tracked_root / "CLAUDE.md").write_text(
        "# Title\n\n" + _PLACEHOLDER_PY, encoding="utf-8"
    )
    dst = tmp_path / "live" / "CLAUDE.md"
    dst.parent.mkdir()
    dst.write_text("# Title\n", encoding="utf-8")  # markerless live (post-install)

    span = SpanEntry(
        anchor="## Python",
        kind=SpanKind.OVERLAY,
        overlay=OverlaySpanPayload(anchor=AnchorAtEndOfFile(), body=body),
    )
    tracked_file = TrackedFile(
        src=Path("CLAUDE.md"),
        dst=str(dst),
        spans=[span],
    )
    cfg = Config(
        tracked_files={"claude_md": tracked_file},
        profiles={"p": Profile(tracked_files=["claude_md"])},
    )
    resolved = ResolvedProfile(tracked_files=["claude_md"])
    ctx = ProfileContext(cfg=cfg, resolved=resolved, repo_root=repo_root, profile="p")

    _install_helpers._deploy_all_tracked_files(
        ctx,
        section_decisions={},
        live_sections_map={},
        host_local_sections_map={
            "claude_md": {
                # The PROJECTION already carries the overlay name (eb070aa).
                HostLocalSectionName("## Python"): HostLocalSection(
                    anchor=AnchorAtEndOfFile(), body=body, body_file=None
                )
            }
        },
    )

    out = dst.read_text(encoding="utf-8")
    assert "setforge:user-section" not in out  # every host-local marker stripped
    assert out.count("## Python") == 1  # injected exactly once, markerless
    assert "use uv" in out
    # The sidecar advanced even though disposition is None.
    states = spans_store.get_states("p", "claude_md")
    assert "## Python" in states


def test_resume_marker_strip_steady_state_plans_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live file with no markers (steady state) plans no resume rewrite."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "note.md"
    live.write_text(_STRIPPED, encoding="utf-8")
    before = live.read_bytes()

    assert _install_helpers._plan_resume_marker_strip(live, _STRIPPED) is None

    assert live.read_bytes() == before


def test_resume_marker_strip_advanced_base_plans_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Base advanced past the seed (marker-bearing) → resume stands down.

    A ``disposition: shared`` file whose tracked content legitimately carries an
    in-content shared marker re-deploys that marker into live on every install,
    and the post-deploy advance re-baselines the stored base to the SAME
    marker-bearing form. On the next install live still carries the marker but
    the base is no longer the markerless seed, so
    ``strip_shared_markers(live) != base``. That is steady state, NOT an
    interrupted migration: the resume must plan no live rewrite (re-stripping
    live would diverge from the advanced base and corrupt the merge ancestor).
    Regression for the second-install crash.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "note.md"
    live.write_text(_LIVE_WITH_MARKERS, encoding="utf-8")
    live.chmod(0o600)
    live_before = live.read_bytes()

    # Base == the marker-BEARING live (advanced), NOT the markerless seed.
    planned = _install_helpers._plan_resume_marker_strip(live, _LIVE_WITH_MARKERS)

    # No rewrite planned: live and the advanced base are both untouched.
    assert planned is None
    assert live.read_bytes() == live_before


def test_disposition_base_reinstall_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disposition:shared markdown with a shared marker re-installs idempotently.

    Drives :func:`_plan_disposition_base` + :func:`_apply_deferred_base_migration`
    through the realistic lifecycle: first install seeds the markerless base,
    then the deploy advances it to the marker-bearing form. A clean (no-drift)
    re-install must return the advanced base verbatim, defer no writes, leave
    live unchanged, and NOT raise. Regression for the second-install ``cannot
    resume marker strip`` crash.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "shared.md"
    live.write_text(_LIVE_WITH_MARKERS, encoding="utf-8")

    # First install: base absent → seed the markerless base from live + strip.
    first = _install_helpers._plan_disposition_base("p", "f", live)
    assert first.migrated is True
    assert first.base_text == _STRIPPED
    _install_helpers._apply_deferred_base_migration("p", "f", live, first)
    assert base_store.read_base("p", "f") == _STRIPPED.encode("utf-8")

    # Simulate the post-deploy advance: live + base re-baselined to the
    # marker-bearing tracked form (tracked legitimately keeps the shared marker).
    live.write_text(_LIVE_WITH_MARKERS, encoding="utf-8")
    base_store.write_base("p", "f", _LIVE_WITH_MARKERS.encode("utf-8"))
    live_before = live.read_bytes()

    # Second install (no drift): returns the advanced base, defers nothing,
    # no rewrite, no raise.
    second = _install_helpers._plan_disposition_base("p", "f", live)
    assert second.migrated is False
    assert second.base_text == _LIVE_WITH_MARKERS
    assert second.deferred_seed is None
    assert second.deferred_live_strip is None
    _install_helpers._apply_deferred_base_migration("p", "f", live, second)
    assert live.read_bytes() == live_before
    assert base_store.read_base("p", "f") == _LIVE_WITH_MARKERS.encode("utf-8")
