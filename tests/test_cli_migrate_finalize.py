"""CLI tests for ``setforge migrate --finalize`` — the tracked-marker strip.

The finalizer strips the now-vestigial HOST_LOCAL user-section markers from
tracked markdown sources, gated on the operator-declared ``minimum_version``
floor being at or above the markerless conversion version. It is preview-
confirmed, idempotent (no-op records NO transition), all-or-nothing across
files, and revertible via the migrate transition log.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import transitions
from setforge.cli import app
from setforge.errors import ConfigError

_HL = (
    "intro\n"
    "<!-- setforge:user-section start host-local HL -->\n"
    "host body\n"
    "<!-- setforge:user-section end host-local HL -->\n"
    "outro\n"
)
_HL_STRIPPED = "intro\noutro\n"
_SHARED = (
    "top\n"
    "<!-- setforge:user-section start shared SH -->\n"
    "shared body\n"
    "<!-- setforge:user-section end shared SH -->\n"
    "bottom\n"
)
_NO_MARKERS = "just plain content\nno markers here\n"
_UNCLOSED = (
    "x\n<!-- setforge:user-section start host-local BAD -->\nbody never closed\n"
)


def _make_repo(tmp_path: Path, *, floor: str | None, files: dict[str, str]) -> Path:
    """Lay down setforge.yaml + tracked/ sources. Return the cfg path.

    ``files`` maps a tracked source filename to its content. resolve_src
    joins ``repo_root / "tracked" / src``.
    """
    tracked = tmp_path / "tracked"
    tracked.mkdir(exist_ok=True)
    lines = ["version: 1", 'schema_version: "1.2"']
    if floor is not None:
        lines.append(f'minimum_version: "{floor}"')
    lines.append("tracked_files:")
    for i, (name, content) in enumerate(files.items()):
        (tracked / name).write_text(content, encoding="utf-8")
        lines += [f"  f{i}:", f"    src: {name}", f"    dst: ~/out-{name}"]
    lines += ["profiles:", "  default: {}"]
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cfg


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the transition log into the test's tmp dir."""
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


def _latest_migrate_transition() -> transitions.TransitionDir | None:
    return transitions.load_latest(transitions.MIGRATE_TRANSITION_PROFILE)


# ---------------------------------------------------------------------------
# gate: refuse unless minimum_version >= conversion version (1.2)
# ---------------------------------------------------------------------------


def test_finalize_refuses_when_floor_unset(tmp_path: Path) -> None:
    cfg = _make_repo(tmp_path, floor=None, files={"a.md": _HL})
    result = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )
    assert result.exit_code != 0, result.output
    assert "minimum_version" in result.output
    assert (tmp_path / "tracked" / "a.md").read_text(encoding="utf-8") == _HL
    assert _latest_migrate_transition() is None


def test_finalize_refuses_when_floor_below_conversion(tmp_path: Path) -> None:
    cfg = _make_repo(tmp_path, floor="1.1", files={"a.md": _HL})
    result = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )
    assert result.exit_code != 0, result.output
    assert "minimum_version" in result.output
    assert (tmp_path / "tracked" / "a.md").read_text(encoding="utf-8") == _HL
    assert _latest_migrate_transition() is None


# ---------------------------------------------------------------------------
# permitted: strip host-local, preserve shared, record one transition
# ---------------------------------------------------------------------------


def test_finalize_strips_host_local_preserves_shared(tmp_path: Path) -> None:
    cfg = _make_repo(tmp_path, floor="1.2", files={"hl.md": _HL, "sh.md": _SHARED})
    result = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "tracked" / "hl.md").read_text(encoding="utf-8") == _HL_STRIPPED
    # SHARED markers untouched.
    assert (tmp_path / "tracked" / "sh.md").read_text(encoding="utf-8") == _SHARED
    assert _latest_migrate_transition() is not None


def test_finalize_skips_non_markdown_sources(tmp_path: Path) -> None:
    """A non-markdown tracked source is ignored even if it has marker-like text."""
    cfg = _make_repo(tmp_path, floor="1.2", files={"hl.md": _HL, "data.json": _HL})
    result = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "tracked" / "data.json").read_text(encoding="utf-8") == _HL
    assert (tmp_path / "tracked" / "hl.md").read_text(encoding="utf-8") == _HL_STRIPPED


# ---------------------------------------------------------------------------
# no-op: nothing to strip records NO transition
# ---------------------------------------------------------------------------


def test_finalize_noop_records_no_transition(tmp_path: Path) -> None:
    cfg = _make_repo(tmp_path, floor="1.2", files={"plain.md": _NO_MARKERS})
    result = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )
    assert result.exit_code == 0, result.output
    assert "no host-local markers to strip" in result.output
    assert (tmp_path / "tracked" / "plain.md").read_text(
        encoding="utf-8"
    ) == _NO_MARKERS
    assert _latest_migrate_transition() is None


def test_finalize_second_run_is_noop(tmp_path: Path) -> None:
    """Re-running after a strip finds nothing and records no second transition."""
    cfg = _make_repo(tmp_path, floor="1.2", files={"hl.md": _HL})
    first = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )
    assert first.exit_code == 0, first.output
    first_tx = _latest_migrate_transition()
    assert first_tx is not None
    second = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )
    assert second.exit_code == 0, second.output
    assert "no host-local markers to strip" in second.output
    # No NEW transition recorded — the latest is still the first one.
    assert _latest_migrate_transition() == first_tx


# ---------------------------------------------------------------------------
# all-or-nothing: a malformed marker aborts the whole batch
# ---------------------------------------------------------------------------


def test_finalize_all_or_nothing_on_marker_error(tmp_path: Path) -> None:
    cfg = _make_repo(tmp_path, floor="1.2", files={"good.md": _HL, "bad.md": _UNCLOSED})
    result = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )
    assert result.exit_code != 0, result.output
    # Neither file mutated — the good file's strip was computed in memory only.
    assert (tmp_path / "tracked" / "good.md").read_text(encoding="utf-8") == _HL
    assert (tmp_path / "tracked" / "bad.md").read_text(encoding="utf-8") == _UNCLOSED
    assert _latest_migrate_transition() is None


# ---------------------------------------------------------------------------
# mutual exclusion + non-interactive guard
# ---------------------------------------------------------------------------


def test_finalize_mutually_exclusive_with_apply(tmp_path: Path) -> None:
    cfg = _make_repo(tmp_path, floor="1.2", files={"hl.md": _HL})
    result = CliRunner().invoke(
        app, ["migrate", "--finalize", "--apply", f"--config={cfg}"]
    )
    assert result.exit_code != 0, result.output
    assert (tmp_path / "tracked" / "hl.md").read_text(encoding="utf-8") == _HL


def test_finalize_non_tty_requires_yes(tmp_path: Path) -> None:
    """Without --yes on a non-TTY stdin, refuse rather than silently apply."""
    cfg = _make_repo(tmp_path, floor="1.2", files={"hl.md": _HL})
    result = CliRunner().invoke(app, ["migrate", "--finalize", f"--config={cfg}"])
    assert result.exit_code != 0, result.output
    assert (tmp_path / "tracked" / "hl.md").read_text(encoding="utf-8") == _HL
    assert _latest_migrate_transition() is None


# ---------------------------------------------------------------------------
# round-trip: revert restores the markers byte-for-byte
# ---------------------------------------------------------------------------


def test_finalize_then_revert_restores_markers(tmp_path: Path) -> None:
    cfg = _make_repo(tmp_path, floor="1.2", files={"hl.md": _HL})
    src = tmp_path / "tracked" / "hl.md"
    original = src.read_bytes()
    fin = CliRunner().invoke(app, ["migrate", "--finalize", "--yes", f"--config={cfg}"])
    assert fin.exit_code == 0, fin.output
    assert src.read_text(encoding="utf-8") == _HL_STRIPPED
    rev = CliRunner().invoke(
        app, ["revert", "--profile=migrate", "--yes", f"--config={cfg}"]
    )
    assert rev.exit_code == 0, rev.output
    assert src.read_bytes() == original


# ---------------------------------------------------------------------------
# the floor also gates migrate's own read paths (detect_current_schema bypass)
# ---------------------------------------------------------------------------


def test_migrate_check_refuses_below_floor(tmp_path: Path) -> None:
    """``migrate --check`` reads via detect_current_schema but still refuses."""
    cfg = _make_repo(tmp_path, floor="1.9", files={"a.md": _NO_MARKERS})
    result = CliRunner().invoke(app, ["migrate", "--check", f"--config={cfg}"])
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, ConfigError)
    assert "minimum_version" in str(result.exception)


def test_migrate_apply_refuses_below_floor_without_mutating(tmp_path: Path) -> None:
    """``migrate --apply`` refuses BEFORE mutating — no below-floor write."""
    cfg = _make_repo(tmp_path, floor="1.9", files={"a.md": _NO_MARKERS})
    before = cfg.read_bytes()
    result = CliRunner().invoke(app, ["migrate", "--apply", "--yes", f"--config={cfg}"])
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, ConfigError)
    assert "minimum_version" in str(result.exception)
    assert cfg.read_bytes() == before
