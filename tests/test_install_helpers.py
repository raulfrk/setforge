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
from setforge.compare import CompareReport, CompareStatus, FileCompare
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


def test_migrate_shared_markers_seeds_base_before_stripping_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash-safe ordering: the base is seeded BEFORE live is rewritten to stripped.

    The seed-first ordering is the crash-safety invariant — a kill between the
    two steps must leave base-present + live-marker-bearing (resumable), never
    base-absent-after-strip. Records the interleaving of ``write_base`` and the
    live rewrite and asserts the base write lands first.
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

    seeded = _install_helpers._migrate_shared_markers_for_base("p", "f", live)

    assert seeded == _STRIPPED
    # base seeded FIRST, live stripped SECOND.
    assert order == ["base", "live"]
    assert base_store.read_base("p", "f") == _STRIPPED.encode("utf-8")
    assert live.read_text(encoding="utf-8") == _STRIPPED


def test_resume_marker_strip_completes_without_reseeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash-resume: a base-present, live-marker-bearing file finishes the strip.

    Reproduces the crash-resume state (base seeded, live still marker-bearing)
    and drives the resume path directly: it rewrites live to the stripped form
    matching the already-seeded base, WITHOUT calling ``write_base`` again.
    """
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "note.md"
    live.write_text(_LIVE_WITH_MARKERS, encoding="utf-8")
    live.chmod(0o600)

    reseeded: list[bytes] = []
    monkeypatch.setattr(
        "setforge.cli._install_helpers.base_store.write_base",
        lambda profile, file_id, data: reseeded.append(data),
    )

    _install_helpers._resume_marker_strip(live, _STRIPPED)

    # Strip landed; mode preserved; NO re-seed.
    assert live.read_text(encoding="utf-8") == _STRIPPED
    assert stat.S_IMODE(live.stat().st_mode) == 0o600
    assert reseeded == []


def test_deploy_preserve_overlay_loads_spans_and_advances_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preserve file with host-local OVERLAY spans deploys markerless + sidecar.

    Regression for the un-gated spans load: before the fix ``file_spans`` was
    populated ONLY when ``disposition is not None``, so a ``preserve_user_sections``
    file carrying host-local overlay spans (disposition=None) deployed WITH markers
    and never advanced its spans sidecar. The gate now keys on ``tracked_file.spans``.
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
        preserve_user_sections=True,
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


def _hl_region(name: str, body: str) -> str:
    return (
        f"<!-- setforge:user-section start host-local {name} -->\n"
        f"{body}"
        f"<!-- setforge:user-section end host-local {name} hash={_HL_HASH} -->\n"
    )


def _claude_md_ctx(repo_root: Path, dst: Path) -> ProfileContext:
    tracked_root = repo_root / "tracked"
    tracked_root.mkdir(parents=True, exist_ok=True)
    (tracked_root / "CLAUDE.md").write_text("# Title\n", encoding="utf-8")
    tracked_file = TrackedFile(
        src=Path("CLAUDE.md"), dst=str(dst), preserve_user_sections=True
    )
    cfg = Config(
        tracked_files={"claude_md": tracked_file},
        profiles={"p": Profile(tracked_files=["claude_md"])},
    )
    resolved = ResolvedProfile(tracked_files=["claude_md"])
    return ProfileContext(cfg=cfg, resolved=resolved, repo_root=repo_root, profile="p")


def test_migrate_host_local_markers_captures_and_drops_empty(tmp_path: Path) -> None:
    """Populated host-local bodies are captured; empty regions produce no overlay."""
    from setforge.source import load_local_tracked_file_overlays

    dst = tmp_path / "live" / "CLAUDE.md"
    dst.parent.mkdir()
    dst.write_text(
        "# Title\n\n"
        + _hl_region("notes", "my per-host notes\n")
        + "\n"
        + _hl_region("blank", ""),
        encoding="utf-8",
    )
    ctx = _claude_md_ctx(tmp_path / "repo", dst)
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files:\n  claude_md: {}\n", encoding="utf-8")

    migration = _install_helpers.migrate_host_local_markers_on_install(
        ctx.cfg, ctx.resolved, ctx.repo_root, local_config_path=local
    )

    assert migration.migrated is True
    assert migration.pre_text == "tracked_files:\n  claude_md: {}\n"
    assert migration.names_by_file == {"claude_md": ["notes"]}  # blank dropped
    overlay = load_local_tracked_file_overlays(local)["claude_md"]
    assert [s.anchor for s in overlay.spans] == ["notes"]
    assert overlay.spans[0].overlay is not None
    assert overlay.spans[0].overlay.body == "my per-host notes\n"


def test_migrate_host_local_markers_noop_when_no_markers(tmp_path: Path) -> None:
    dst = tmp_path / "live" / "CLAUDE.md"
    dst.parent.mkdir()
    dst.write_text("# Title\n\nplain content\n", encoding="utf-8")
    ctx = _claude_md_ctx(tmp_path / "repo", dst)
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files:\n  claude_md: {}\n", encoding="utf-8")
    before = local.read_bytes()

    migration = _install_helpers.migrate_host_local_markers_on_install(
        ctx.cfg, ctx.resolved, ctx.repo_root, local_config_path=local
    )

    assert migration.migrated is False
    assert local.read_bytes() == before  # no write


def test_migrate_host_local_markers_crash_resume_no_double_write(
    tmp_path: Path,
) -> None:
    """Overlay present + live still markered → no second span, migrated=False."""
    dst = tmp_path / "live" / "CLAUDE.md"
    dst.parent.mkdir()
    dst.write_text(
        "# Title\n\n" + _hl_region("notes", "my per-host notes\n"), encoding="utf-8"
    )
    ctx = _claude_md_ctx(tmp_path / "repo", dst)
    local = tmp_path / "local.yaml"
    local.write_text("tracked_files:\n  claude_md: {}\n", encoding="utf-8")

    first = _install_helpers.migrate_host_local_markers_on_install(
        ctx.cfg, ctx.resolved, ctx.repo_root, local_config_path=local
    )
    assert first.migrated is True
    after_first = local.read_bytes()

    # Re-run with the SAME live markers still present (deploy never ran).
    second = _install_helpers.migrate_host_local_markers_on_install(
        ctx.cfg, ctx.resolved, ctx.repo_root, local_config_path=local
    )
    assert second.migrated is False  # presence-check skipped the existing span
    assert local.read_bytes() == after_first  # no duplicate span, no write


def test_seed_host_local_marker_snapshot_earliest_wins(tmp_path: Path) -> None:
    local = tmp_path / "local.yaml"
    mig = _install_helpers.HostLocalMarkerMigration(
        local_path=local, pre_text="MINE", migrated=True, names_by_file={"d": ["n"]}
    )

    # 10.2 already migrated + seeded file_pre → keep the 10.2 (pre-everything) text.
    ten_two_migrated = _install_helpers.OverlaySpanMigration(
        path=local, pre_text="TEN_TWO", migrated=True
    )
    dst_paths: list[Path] = []
    file_pre: dict[Path, str | None] = {local: "TEN_TWO"}
    _install_helpers.seed_host_local_marker_snapshot(
        mig, ten_two_migrated, dst_paths, file_pre
    )
    assert file_pre[local] == "TEN_TWO"
    assert local in dst_paths

    # 10.2 did NOT migrate → our pre_text is the pre-everything text.
    ten_two_noop = _install_helpers.OverlaySpanMigration(
        path=local, pre_text=None, migrated=False
    )
    dst_paths2: list[Path] = []
    file_pre2: dict[Path, str | None] = {}
    _install_helpers.seed_host_local_marker_snapshot(
        mig, ten_two_noop, dst_paths2, file_pre2
    )
    assert file_pre2[local] == "MINE"
    assert local in dst_paths2


def test_resume_marker_strip_steady_state_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live file with no markers (steady state) is left untouched by the resume."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "note.md"
    live.write_text(_STRIPPED, encoding="utf-8")
    before = live.read_bytes()

    _install_helpers._resume_marker_strip(live, _STRIPPED)

    assert live.read_bytes() == before
