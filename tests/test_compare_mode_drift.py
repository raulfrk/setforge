"""Tests for the ``mode_drift`` channel on :class:`setforge.compare.FileCompare`.

The contract:

- When ``tracked_file.mode`` is ``None``, ``mode_drift`` is always
  False — the drift axis is opt-in per tracked_file.
- When ``tracked_file.mode`` is set and the live dst's permission bits
  (via :func:`stat.S_IMODE`) match, ``mode_drift`` is False AND
  ``status`` is :attr:`CompareStatus.UNCHANGED`.
- When ``tracked_file.mode`` is set and the live dst's perms drift,
  ``mode_drift`` is True AND ``status`` is :attr:`CompareStatus.DRIFTED`.
- ``mode_drift`` contributes to the "unexpected drift" axis that
  ``compare --check`` exits non-zero on (it's a contract violation,
  not user-opt-in drift like preserve_user_keys).
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from setforge.cli import _install_helpers
from setforge.cli._confirm import AutoDirection, confirm_auto_operation
from setforge.compare import (
    CompareReport,
    CompareStatus,
    FileCompare,
    _compare_one,
)
from setforge.config import TrackedFile
from setforge.errors import ConfirmRequiresInteractive


def _make(src: Path, dst: Path, *, mode: int | None) -> TrackedFile:
    return TrackedFile.model_validate({"src": str(src), "dst": str(dst), "mode": mode})


def test_mode_drift_false_when_mode_unset(tmp_path: Path) -> None:
    """``mode: None`` -> drift axis disabled, always False."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o600)
    tf = _make(src, dst, mode=None)

    entry, _ = _compare_one("foo", src, dst, tf)

    assert entry.mode_drift is False
    assert entry.status is CompareStatus.UNCHANGED


def test_mode_drift_false_when_live_matches_declared(tmp_path: Path) -> None:
    """Declared mode equals live mode -> drift False, UNCHANGED."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o755)
    tf = _make(src, dst, mode=0o755)

    entry, unexpected = _compare_one("foo", src, dst, tf)

    assert entry.mode_drift is False
    assert entry.status is CompareStatus.UNCHANGED
    assert unexpected is False


def test_mode_drift_true_after_manual_chmod(tmp_path: Path) -> None:
    """Declared ``0o755``; user runs ``chmod 0o644`` on dst -> drift detected."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o644)
    tf = _make(src, dst, mode=0o755)

    entry, unexpected = _compare_one("foo", src, dst, tf)

    assert entry.mode_drift is True
    assert entry.status is CompareStatus.DRIFTED
    # mode_drift flows into the "unexpected" axis -> compare --check exits !=0
    assert unexpected is True


def test_mode_drift_uses_s_imode_not_raw_st_mode(tmp_path: Path) -> None:
    """The compare side uses :func:`stat.S_IMODE` to strip ``S_IFREG``
    high-bits — raw ``st_mode`` would always differ from a 12-bit
    perm-only literal.
    """
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o640)
    tf = _make(src, dst, mode=0o640)

    entry, _ = _compare_one("foo", src, dst, tf)

    # The PROOF this works: status is UNCHANGED. A raw st_mode ==
    # comparison would always be False (st_mode carries S_IFREG = 0o100000).
    assert entry.mode_drift is False
    assert entry.status is CompareStatus.UNCHANGED


def test_content_drift_and_mode_drift_compose(tmp_path: Path) -> None:
    """Independent drift axes — both can flag simultaneously."""
    src = tmp_path / "src"
    src.write_text("source-content\n")
    dst = tmp_path / "dst"
    dst.write_text("live-content\n")
    dst.chmod(0o600)
    tf = _make(src, dst, mode=0o755)

    entry, unexpected = _compare_one("foo", src, dst, tf)

    assert entry.diff != ""  # content drift
    assert entry.mode_drift is True  # mode drift
    assert entry.status is CompareStatus.DRIFTED
    assert unexpected is True


def test_install_gate_catches_mode_drift_only() -> None:
    """A DRIFTED entry whose only drift channel is ``mode_drift`` (no
    content diff, no unexpected_drift_keys) MUST still trip the install
    drift gate. Pre-fix the gate keyed only on ``unexpected_drift_keys``
    and silently let mode-only drift through (declared mode + matching
    content + wrong live mode would deploy without surfacing the
    perms-drift).
    """
    entry = FileCompare(
        name="hook_script",
        status=CompareStatus.DRIFTED,
        diff="",
        mode_drift=True,
    )
    report = CompareReport(entries=[entry], has_unexpected_drift=True)

    # The gate only reaches ctx.profile when rendering the actionable
    # error message; a SimpleNamespace stub anchors the attribute access
    # without forcing a full ProfileContext construction in unit scope.
    ctx_stub = SimpleNamespace(profile="test-mode")

    with pytest.raises(typer.Exit) as excinfo:
        _install_helpers._check_unexpected_drift(
            report,
            ctx_stub,  # type: ignore[arg-type]
            auto_accept_tracked=False,
            auto_accept_live=False,
        )

    assert excinfo.value.exit_code == 1


def test_compare_one_populates_mode_values(tmp_path: Path) -> None:
    """``_compare_one`` records the live + tracked mode bits for the plan line."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o644)
    tf = _make(src, dst, mode=0o755)

    entry, _ = _compare_one("foo", src, dst, tf)

    assert entry.mode_drift is True
    assert entry.live_mode == 0o644
    assert entry.tracked_mode == 0o755


def test_compare_one_mode_values_none_when_mode_unset(tmp_path: Path) -> None:
    """No declared ``mode:`` -> both mode values stay ``None`` (no axis)."""
    src = tmp_path / "src"
    src.write_text("x\n")
    dst = tmp_path / "dst"
    dst.write_text("x\n")
    dst.chmod(0o600)
    tf = _make(src, dst, mode=None)

    entry, _ = _compare_one("foo", src, dst, tf)

    assert entry.live_mode is None
    assert entry.tracked_mode is None


def _mode_drift_report() -> CompareReport:
    """A single mode-only-drift entry (0o644 live, 0o755 tracked)."""
    entry = FileCompare(
        name="hook_script",
        status=CompareStatus.DRIFTED,
        diff="",
        mode_drift=True,
        live_mode=0o644,
        tracked_mode=0o755,
    )
    return CompareReport(entries=[entry], has_unexpected_drift=True)


def test_build_plan_surfaces_mode_drift_as_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mode-only-drift entry yields a NON-empty plan (a mode-transition risk).

    Pre-fix the plan filtered on ``unexpected_drift_keys`` and returned an
    empty plan for mode drift; ``confirm_auto_operation`` auto-proceeds on an
    empty plan, so deploy silently reapplied the tracked mode. The risk line
    makes the plan non-empty so the confirm gate fires.
    """
    report = _mode_drift_report()
    ctx_stub = SimpleNamespace(profile="test-mode")
    monkeypatch.setattr(
        _install_helpers,
        "_resolve_drift_paths",
        lambda _report, _ctx: [
            (report.entries[0], Path("/tracked/hook"), Path("/live/hook"))
        ],
    )

    plan = _install_helpers._build_unexpected_drift_plan(
        drift_report=report,
        ctx=ctx_stub,  # type: ignore[arg-type]
        direction=AutoDirection.TRACKED_TO_LIVE,
    )

    # Mode-only drift carries no content keys, so no file-change rows...
    assert plan.file_changes == ()
    # ...but a non-empty risk line naming the live → tracked transition.
    assert plan.risks
    assert any("0o644" in r and "0o755" in r for r in plan.risks)
    assert any("/live/hook" in r for r in plan.risks)


def test_mode_drift_plan_gates_confirm_when_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-empty mode-drift plan reaches the confirm gate instead of
    auto-proceeding: a non-TTY confirm raises rather than silently returning
    True (which is what an empty plan would do).
    """
    report = _mode_drift_report()
    ctx_stub = SimpleNamespace(profile="test-mode")
    monkeypatch.setattr(
        _install_helpers,
        "_resolve_drift_paths",
        lambda _report, _ctx: [
            (report.entries[0], Path("/tracked/hook"), Path("/live/hook"))
        ],
    )
    plan = _install_helpers._build_unexpected_drift_plan(
        drift_report=report,
        ctx=ctx_stub,  # type: ignore[arg-type]
        direction=AutoDirection.TRACKED_TO_LIVE,
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(ConfirmRequiresInteractive):
        confirm_auto_operation(
            command="install --auto-accept-tracked",
            profile="test-mode",
            plan=plan,
            yes=False,
        )


def test_bare_install_mode_drift_message_is_permission_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The bare-install reject message names permission-mode drift, not the
    removed ``setforge merge`` command.
    """
    report = _mode_drift_report()
    ctx_stub = SimpleNamespace(profile="test-mode")

    with pytest.raises(typer.Exit) as excinfo:
        _install_helpers._check_unexpected_drift(
            report,
            ctx_stub,  # type: ignore[arg-type]
            auto_accept_tracked=False,
            auto_accept_live=False,
        )

    assert excinfo.value.exit_code == 1
    message = capsys.readouterr().err
    assert "permission-mode drift" in message
    assert "merge" not in message
    assert "--auto-accept-tracked" in message
