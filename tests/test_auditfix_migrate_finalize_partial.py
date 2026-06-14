"""Regression: ``setforge migrate --finalize`` is all-or-nothing on write errors.

The finalize strip computes every plan in memory then writes each with
``atomic_write_text`` in a loop. atomic_write_text is per-file atomic, but the
BATCH was not transactional: if writing file N raised (disk full, EACCES,
read-only mount), the exception propagated AFTER files 1..N-1 were already
mutated, and the revertible transition (recorded only after the loop) never ran
— leaving the tracked tree half-stripped with NO revert path.

The fix snapshots raw bytes up front and rolls the whole batch back on any
``OSError``, mirroring the ``--apply`` path's mid-chain rollback. These tests
fail pre-fix (file 1 stays stripped, exit may be nonzero but no rollback) and
pass post-fix (file 1 restored, exit nonzero, no transition).
"""

from __future__ import annotations

from pathlib import Path

import click.testing
import pytest
from typer.testing import CliRunner

from setforge import atomicio, transitions
from setforge.cli import app

_HL_A = (
    "intro-a\n"
    "<!-- setforge:user-section start host-local A -->\n"
    "host body a\n"
    "<!-- setforge:user-section end host-local A -->\n"
    "outro-a\n"
)
_HL_B = (
    "intro-b\n"
    "<!-- setforge:user-section start host-local B -->\n"
    "host body b\n"
    "<!-- setforge:user-section end host-local B -->\n"
    "outro-b\n"
)


def _make_repo(tmp_path: Path, *, files: dict[str, str]) -> Path:
    tracked = tmp_path / "tracked"
    tracked.mkdir(exist_ok=True)
    lines = ["version: 1", 'schema_version: "1.2"', 'minimum_version: "1.2"']
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
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))


def _latest_migrate_transition() -> transitions.TransitionDir | None:
    return transitions.load_latest(transitions.MIGRATE_TRANSITION_PROFILE)


def test_finalize_rolls_back_whole_batch_on_midloop_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write OSError on the 2nd file restores the 1st and records no transition."""
    cfg = _make_repo(tmp_path, files={"a.md": _HL_A, "b.md": _HL_B})
    src_a = tmp_path / "tracked" / "a.md"
    src_b = tmp_path / "tracked" / "b.md"
    pre_a = src_a.read_bytes()
    pre_b = src_b.read_bytes()

    real_write = atomicio.atomic_write_text
    calls: list[Path] = []

    def fake_write(path: Path, text: str) -> None:
        calls.append(path)
        # Let the first file's write land for real, then fail the second so the
        # batch is genuinely half-applied at the point of failure.
        if len(calls) >= 2:
            raise OSError("simulated disk full on second write")
        real_write(path, text)

    monkeypatch.setattr("setforge.cli.migrate.atomicio.atomic_write_text", fake_write)

    result: click.testing.Result = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )

    assert result.exit_code != 0, result.output
    # At least the second write was attempted (the failure point).
    assert len(calls) >= 2
    # Whole batch rolled back: BOTH files back to their pre-strip bytes.
    assert src_a.read_bytes() == pre_a, "first file not rolled back"
    assert src_b.read_bytes() == pre_b, "second file mutated despite failure"
    # No revertible transition recorded for a failed batch.
    assert _latest_migrate_transition() is None


def test_finalize_first_write_failure_leaves_all_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError on the very first write leaves every tracked file untouched."""
    cfg = _make_repo(tmp_path, files={"a.md": _HL_A, "b.md": _HL_B})
    src_a = tmp_path / "tracked" / "a.md"
    src_b = tmp_path / "tracked" / "b.md"
    pre_a = src_a.read_bytes()
    pre_b = src_b.read_bytes()

    def fake_write(path: Path, text: str) -> None:
        raise OSError("simulated read-only mount")

    monkeypatch.setattr("setforge.cli.migrate.atomicio.atomic_write_text", fake_write)

    result: click.testing.Result = CliRunner().invoke(
        app, ["migrate", "--finalize", "--yes", f"--config={cfg}"]
    )

    assert result.exit_code != 0, result.output
    assert src_a.read_bytes() == pre_a
    assert src_b.read_bytes() == pre_b
    assert _latest_migrate_transition() is None
