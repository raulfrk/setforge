"""Tests for the CONFLICTED drift-class slot (forked-scalar conflicts).

Covers:
- CONFLICTED: a forked structural file where the stored base differs from
  BOTH live and tracked at the same scalar path (the shape the next
  interactive install would prompt on)
- ``forked_scalar_conflicts`` renders each conflict as
  ``path: base → tracked | live`` (tracked = upstream, live = yours)
- conflicted drift is unexpected (``--check`` exits 1)
- auto-resolvable forked drift (base equals one side) stays out of the
  CONFLICTED slot (EXPECTED / STALE; ``--check`` exits 0)
- a SHARED file conflicting at a FORKED span path is CONFLICTED; the same
  conflict at a non-span path stays UNEXPECTED
- no stored base / non-structural file / torn base store → the slot is
  inert and later slots classify as before
- --json schema: ``forked_scalar_conflicts`` populated for a conflict
- summary table Why column carries the conflict line
"""

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from setforge import base_store
from setforge.cli.compare import _compare_json_data
from setforge.compare import (
    CompareStatus,
    DriftClass,
    compare_profile,
    compare_summary_table,
)
from setforge.config import Config, Profile, TrackedFile
from setforge.errors import BaseStoreError


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_config(tracked_file: TrackedFile) -> Config:
    return Config(
        tracked_files={"x": tracked_file},
        profiles={"p": Profile(tracked_files=["x"])},
    )


_BASE_BODY = "trackedKey: tracked-value\nuserKeyA: placeholder-A\n"
_LIVE_CONFLICT = _BASE_BODY.replace("placeholder-A", "live-edit")
_TRACKED_CONFLICT = _BASE_BODY.replace("placeholder-A", "tracked-edit")

_CONFLICT_LINE = "userKeyA: placeholder-A → tracked-edit | live-edit"


def _structural_file(
    tmp_path: Path,
    *,
    tracked: str,
    live: str,
    disposition: str = "forked",
    spans: list[dict[str, str]] | None = None,
    suffix: str = ".yaml",
) -> tuple[Config, Path]:
    """One-file structural config; returns (config, repo_root)."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / f"x{suffix}", tracked)
    dst = tmp_path / "live" / f"x{suffix}"
    _write(dst, live)
    data: dict[str, object] = {
        "src": f"x{suffix}",
        "dst": str(dst),
        "disposition": disposition,
    }
    if spans is not None:
        data["spans"] = spans
    return _make_config(TrackedFile.model_validate(data)), repo


def _write_cli_config(tmp_path: Path, *, tracked: str, live: str) -> Path:
    """Forked-disposition one-file setforge.yaml; returns its path."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x.yaml", tracked)
    dst = tmp_path / "live" / "x.yaml"
    _write(dst, live)
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x.yaml\n    dst: {dst}\n"
        "    disposition: forked\nprofiles:\n  p:\n    tracked_files: [x]\n",
        encoding="utf-8",
    )
    return cfg_path


# ---------------------------------------------------------------------------
# CONFLICTED — base ≠ live AND base ≠ tracked at one scalar path
# ---------------------------------------------------------------------------


def test_genuine_conflict_classifies_conflicted(tmp_path: Path) -> None:
    """base ≠ live AND base ≠ tracked at a forked scalar path → CONFLICTED,
    with the 3-value rendering in both the conflicts list and the reason."""
    config, repo = _structural_file(
        tmp_path, tracked=_TRACKED_CONFLICT, live=_LIVE_CONFLICT
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_class is DriftClass.CONFLICTED
    assert entry.forked_scalar_conflicts == [_CONFLICT_LINE]
    assert entry.reason is not None
    assert _CONFLICT_LINE in entry.reason
    assert report.has_unexpected_drift is True


def test_conflicted_check_exits_1(tmp_path: Path) -> None:
    """A genuine forked-scalar conflict fails compare --check."""
    from typer.testing import CliRunner

    from setforge.cli import app

    cfg_path = _write_cli_config(
        tmp_path, tracked=_TRACKED_CONFLICT, live=_LIVE_CONFLICT
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 1, result.output


def test_absent_base_side_renders_absent_marker(tmp_path: Path) -> None:
    """A key absent in base but added divergently on both sides conflicts,
    rendering the base operand as ``(absent)``."""
    live = _BASE_BODY + "userKeyB: live-add\n"
    tracked = _BASE_BODY + "userKeyB: tracked-add\n"
    config, repo = _structural_file(tmp_path, tracked=tracked, live=live)
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.CONFLICTED
    assert entry.forked_scalar_conflicts == [
        "userKeyB: (absent) → tracked-add | live-add"
    ]


def test_non_string_scalars_render_json_tokens(tmp_path: Path) -> None:
    """Non-string scalars render as JSON tokens (1, true, null), not reprs."""
    config, repo = _structural_file(
        tmp_path, tracked="userKeyA: 3\n", live="userKeyA: 2\n"
    )
    base_store.write_base("p", "x", b"userKeyA: 1\n")

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.CONFLICTED
    assert entry.forked_scalar_conflicts == ["userKeyA: 1 → 3 | 2"]


# ---------------------------------------------------------------------------
# auto-resolvable forked drift — base equals one side, no conflict
# ---------------------------------------------------------------------------


def test_live_edit_only_stays_expected(tmp_path: Path) -> None:
    """base == tracked with live edited: the merge auto-keeps live, so the
    forked file stays EXPECTED with no conflicts."""
    config, repo = _structural_file(tmp_path, tracked=_BASE_BODY, live=_LIVE_CONFLICT)
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.EXPECTED
    assert entry.forked_scalar_conflicts == []
    assert report.has_unexpected_drift is False


def test_tracked_advance_only_classifies_stale(tmp_path: Path) -> None:
    """base == live with tracked advanced: auto-resolvable toward tracked —
    the STALE slot still wins (no phantom conflict)."""
    config, repo = _structural_file(
        tmp_path, tracked=_TRACKED_CONFLICT, live=_BASE_BODY
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.STALE
    assert entry.forked_scalar_conflicts == []
    assert report.has_unexpected_drift is False


def test_auto_resolvable_check_exits_0(tmp_path: Path) -> None:
    """Auto-resolvable forked drift passes compare --check."""
    from typer.testing import CliRunner

    from setforge.cli import app

    cfg_path = _write_cli_config(tmp_path, tracked=_BASE_BODY, live=_LIVE_CONFLICT)
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# SHARED files — only FORKED span paths reach the CONFLICTED slot
# ---------------------------------------------------------------------------


def test_shared_forked_span_conflict_classifies_conflicted(tmp_path: Path) -> None:
    """A SHARED file conflicting exactly at a FORKED span path is CONFLICTED
    (without slot 1 it would pass as span-only EXPECTED while install prompts)."""
    config, repo = _structural_file(
        tmp_path,
        tracked=_TRACKED_CONFLICT,
        live=_LIVE_CONFLICT,
        disposition="shared",
        spans=[{"anchor": "userKeyA", "kind": "forked"}],
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.CONFLICTED
    assert entry.forked_scalar_conflicts == [_CONFLICT_LINE]
    assert report.has_unexpected_drift is True


def test_shared_non_span_conflict_stays_unexpected(tmp_path: Path) -> None:
    """The same divergence on a SHARED file WITHOUT a forked span is plain
    UNEXPECTED — the slot never claims non-forked paths."""
    config, repo = _structural_file(
        tmp_path,
        tracked=_TRACKED_CONFLICT,
        live=_LIVE_CONFLICT,
        disposition="shared",
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.UNEXPECTED
    assert entry.forked_scalar_conflicts == []


# ---------------------------------------------------------------------------
# slot inert — no base / non-structural / torn store
# ---------------------------------------------------------------------------


def test_no_base_keeps_forked_expected(tmp_path: Path) -> None:
    """No stored base: nothing to 3-way against — forked drift stays EXPECTED."""
    config, repo = _structural_file(
        tmp_path, tracked=_TRACKED_CONFLICT, live=_LIVE_CONFLICT
    )

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.EXPECTED
    assert entry.forked_scalar_conflicts == []


def test_non_structural_forked_file_unaffected(tmp_path: Path) -> None:
    """Markdown (line-based) forked drift never reaches the scalar slot."""
    config, repo = _structural_file(
        tmp_path,
        tracked="# Title\ntracked-edit\n",
        live="# Title\nlive-edit\n",
        suffix=".md",
    )
    base_store.write_base("p", "x", b"# Title\nbase\n")

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.EXPECTED
    assert entry.forked_scalar_conflicts == []


def test_torn_base_store_degrades_to_no_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A torn base-store read degrades the probe to no-conflict — the entry
    classifies deterministically via the later slots, no crash."""

    def _boom(profile: str, file_id: str) -> bytes | None:
        raise BaseStoreError("torn state")

    monkeypatch.setattr(base_store, "read_base", _boom)
    config, repo = _structural_file(
        tmp_path, tracked=_TRACKED_CONFLICT, live=_LIVE_CONFLICT
    )

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_class is DriftClass.EXPECTED
    assert entry.forked_scalar_conflicts == []


def test_unparsable_live_degrades_to_no_conflict(tmp_path: Path) -> None:
    """A live file that no longer parses as YAML degrades the probe to
    no-conflict instead of crashing compare."""
    config, repo = _structural_file(
        tmp_path, tracked=_TRACKED_CONFLICT, live="a: [unclosed\n"
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.forked_scalar_conflicts == []
    assert entry.drift_class is DriftClass.EXPECTED


# ---------------------------------------------------------------------------
# --json schema
# ---------------------------------------------------------------------------


def test_json_conflicted_entry_schema(tmp_path: Path) -> None:
    """--json carries drift_class=="conflicted" plus the populated
    forked_scalar_conflicts list of pre-rendered strings."""
    config, repo = _structural_file(
        tmp_path, tracked=_TRACKED_CONFLICT, live=_LIVE_CONFLICT
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    data = _compare_json_data(report)
    entry_json = data["entries"][0]

    required = {
        "name",
        "status",
        "disposition",
        "drift_class",
        "reason",
        "span_only_drift",
        "forked_scalar_conflicts",
        "drift_is_expected",
    }
    assert required <= entry_json.keys()
    assert entry_json["drift_class"] == "conflicted"
    assert entry_json["forked_scalar_conflicts"] == [_CONFLICT_LINE]
    assert all(isinstance(c, str) for c in entry_json["forked_scalar_conflicts"])
    assert data["has_unexpected_drift"] is True
    # round-trips through json.dumps without custom encoders
    json.dumps(data)


# ---------------------------------------------------------------------------
# summary table
# ---------------------------------------------------------------------------


def test_summary_table_renders_conflict_line(tmp_path: Path) -> None:
    """The rendered table carries the conflict tokens (render smoke-test).

    Asserts the class tag and the operands appear in the output — not
    the full ordered line or its column placement.
    """
    config, repo = _structural_file(
        tmp_path, tracked=_TRACKED_CONFLICT, live=_LIVE_CONFLICT
    )
    base_store.write_base("p", "x", _BASE_BODY.encode("utf-8"))

    report = compare_profile(config, "p", repo)
    table = compare_summary_table(report)
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, markup=False, no_color=True, width=160)
    console.print(table)
    output = buf.getvalue()

    assert "conflicted" in output, output
    assert "userKeyA" in output, output
    assert "tracked-edit" in output, output
    assert "live-edit" in output, output
