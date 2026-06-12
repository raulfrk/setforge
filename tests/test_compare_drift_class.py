"""Tests for the per-file drift-class axis on compare.

Covers:
- STALE: live == stored base AND tracked != base (the stale-deploy shape
  where a 0/0 drift row used to mask a tracked-side advance)
- stale drift is NOT unexpected (``--check`` exits 0; ``--check --strict``
  still fails)
- EXPECTED / UNEXPECTED slots per the classification precedence
- torn base-store reads degrade to UNEXPECTED instead of crashing
- drift_class is None off the DRIFTED status
- --json schema: new fields present, dead key arrays gone
- summary table renders File | Disposition | Class | Why
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


def _make_config(
    tracked_file: TrackedFile,
    key: str = "x",
    profile_name: str = "p",
) -> Config:
    return Config(
        tracked_files={key: tracked_file},
        profiles={profile_name: Profile(tracked_files=[key])},
    )


def _shared_file(tmp_path: Path, *, tracked: str, live: str) -> tuple[Config, Path]:
    """Build a one-file shared-disposition config; returns (config, repo_root)."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", tracked)
    dst = tmp_path / "live" / "x"
    _write(dst, live)
    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "shared"}
        )
    )
    return config, repo


def _write_cli_config(tmp_path: Path, *, tracked: str, live: str) -> Path:
    """Write a one-file shared-disposition setforge.yaml; returns its path."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", tracked)
    dst = tmp_path / "live" / "x"
    _write(dst, live)
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        f"version: 1\ntracked_files:\n  x:\n    src: x\n    dst: {dst}\n"
        "    disposition: shared\nprofiles:\n  p:\n    tracked_files: [x]\n",
        encoding="utf-8",
    )
    return cfg_path


# ---------------------------------------------------------------------------
# STALE — live == stored base, tracked advanced
# ---------------------------------------------------------------------------


def test_stale_when_live_equals_base_and_tracked_advanced(tmp_path: Path) -> None:
    """live == stored base AND tracked != base classifies STALE with a reason."""
    config, repo = _shared_file(tmp_path, tracked="tracked v2\n", live="tracked v1\n")
    base_store.write_base("p", "x", b"tracked v1\n")

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_class is DriftClass.STALE
    assert entry.reason is not None
    assert "install will update" in entry.reason


def test_stale_is_not_unexpected_check_exits_0(tmp_path: Path) -> None:
    """Stale-only drift is not unexpected: report flag off, --check exits 0."""
    from typer.testing import CliRunner

    from setforge.cli import app

    cfg_path = _write_cli_config(tmp_path, tracked="tracked v2\n", live="tracked v1\n")
    base_store.write_base("p", "x", b"tracked v1\n")

    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 0, result.output


def test_unexpected_drift_check_exits_1(tmp_path: Path) -> None:
    """Real unexpected drift (live edited away from base) still fails --check."""
    from typer.testing import CliRunner

    from setforge.cli import app

    cfg_path = _write_cli_config(tmp_path, tracked="tracked v1\n", live="live edit\n")

    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 1, result.output


def test_check_strict_exits_1_on_stale_only(tmp_path: Path) -> None:
    """--check --strict keeps failing on ANY drift, stale included."""
    from typer.testing import CliRunner

    from setforge.cli import app

    cfg_path = _write_cli_config(tmp_path, tracked="tracked v2\n", live="tracked v1\n")
    base_store.write_base("p", "x", b"tracked v1\n")

    runner = CliRunner()
    result = runner.invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check", "--strict"]
    )
    assert result.exit_code == 1, result.output


def test_no_base_falls_through_to_unexpected(tmp_path: Path) -> None:
    """No stored base: the stale probe finds nothing and shared drift stays
    UNEXPECTED."""
    config, repo = _shared_file(tmp_path, tracked="tracked v2\n", live="tracked v1\n")

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_class is DriftClass.UNEXPECTED
    assert report.has_unexpected_drift is True


def test_torn_base_store_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base-store read error degrades the stale probe to False, no crash."""

    def _boom(profile: str, file_id: str) -> bytes | None:
        raise BaseStoreError("torn state")

    monkeypatch.setattr(base_store, "read_base", _boom)
    config, repo = _shared_file(tmp_path, tracked="tracked v2\n", live="tracked v1\n")

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_class is DriftClass.UNEXPECTED


# ---------------------------------------------------------------------------
# EXPECTED / UNEXPECTED slots
# ---------------------------------------------------------------------------


def test_forked_disposition_classified_expected(tmp_path: Path) -> None:
    """Forked drift lands in the EXPECTED slot with no reason."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "tracked\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "live\n")
    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "forked"}
        )
    )

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.drift_class is DriftClass.EXPECTED
    assert entry.reason is None
    assert report.has_unexpected_drift is False


def test_mode_only_drift_not_stale(tmp_path: Path) -> None:
    """Mode-only drift (content == base == tracked) must NOT classify STALE —
    tracked has not advanced; the perms drifted."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "same\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "same\n")
    dst.chmod(0o600)
    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "shared", "mode": 0o755}
        )
    )
    base_store.write_base("p", "x", b"same\n")

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.mode_drift is True
    assert entry.drift_class is DriftClass.UNEXPECTED


def test_drift_class_none_for_unchanged_and_missing(tmp_path: Path) -> None:
    """drift_class stays None off the DRIFTED status (UNCHANGED + MISSING)."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "same\n")
    dst = tmp_path / "live" / "x"
    _write(dst, "same\n")
    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "shared"}
        )
    )
    report = compare_profile(config, "p", repo)
    assert report.entries[0].status is CompareStatus.UNCHANGED
    assert report.entries[0].drift_class is None
    assert report.entries[0].reason is None

    dst.unlink()
    report = compare_profile(config, "p", repo)
    assert report.entries[0].status is CompareStatus.MISSING
    assert report.entries[0].drift_class is None


def test_missing_still_sets_has_unexpected_drift(tmp_path: Path) -> None:
    """MISSING keeps its existing contract: flagged as unexpected drift."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "x", "data\n")
    dst = tmp_path / "live" / "x"
    config = _make_config(
        TrackedFile.model_validate(
            {"src": "x", "dst": str(dst), "disposition": "shared"}
        )
    )

    report = compare_profile(config, "p", repo)

    assert report.entries[0].status is CompareStatus.MISSING
    assert report.has_unexpected_drift is True


# ---------------------------------------------------------------------------
# --json schema
# ---------------------------------------------------------------------------


def test_json_required_keys_subset(tmp_path: Path) -> None:
    """--json entries carry the new schema keys; dead key arrays are gone."""
    config, repo = _shared_file(tmp_path, tracked="tracked v2\n", live="tracked v1\n")
    base_store.write_base("p", "x", b"tracked v1\n")

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
    assert "expected_drift_keys" not in entry_json
    assert "unexpected_drift_keys" not in entry_json
    assert entry_json["drift_class"] == "stale"
    assert "install will update" in entry_json["reason"]
    assert entry_json["forked_scalar_conflicts"] == []
    # round-trips through json.dumps without custom encoders
    json.dumps(data)


# ---------------------------------------------------------------------------
# summary table
# ---------------------------------------------------------------------------


def test_summary_table_columns(tmp_path: Path) -> None:
    """Table renders File | Disposition | Class | Why with the stale reason."""
    config, repo = _shared_file(tmp_path, tracked="tracked v2\n", live="tracked v1\n")
    base_store.write_base("p", "x", b"tracked v1\n")

    report = compare_profile(config, "p", repo)
    table = compare_summary_table(report)
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, markup=False, no_color=True, width=120)
    console.print(table)
    output = buf.getvalue()

    for header in ("File", "Disposition", "Class", "Why"):
        assert header in output, output
    assert "stale" in output, output
    assert "shared" in output, output
    assert "install will update" in output, output
