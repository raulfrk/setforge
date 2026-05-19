"""Unit tests for :mod:`setforge.cli.upgrade` (radiolist + subprocess mocked)."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge._pypi_client import PyPIVersionInfo
from setforge.cli import app
from setforge.cli import upgrade as upgrade_mod
from setforge.cli.upgrade import (
    SchemaChangeAssessment,
    SchemaChangeKind,
    UpgradeChoice,
    UpgradePlan,
    _assess_schema_change,
    _build_upgrade_plan,
    _confirm_upgrade,
)


# ---------------------------------------------------------------------------
# Schema-change assessment
# ---------------------------------------------------------------------------


def test_assess_schema_change_none_when_minor_bump_with_clean_notes() -> None:
    notes = "### Added\n- new flag --foo\n### Fixed\n- bug"
    out = _assess_schema_change(notes, current_schema="1.0", is_major_bump=False)
    assert out.kind is SchemaChangeKind.NONE
    assert out.to_schema is None
    assert "No schema change" in out.impact_summary


def test_assess_schema_change_detected_from_schema_version_bumped_line() -> None:
    notes = (
        "### Changed\n"
        "- schema_version bumped 1.0 → 1.1 (additive)\n"
        "- adds: tracked_files.<id>.mode\n"
    )
    out = _assess_schema_change(notes, current_schema="1.0", is_major_bump=False)
    assert out.kind is SchemaChangeKind.DETECTED
    assert out.from_schema == "1.0"
    assert out.to_schema == "1.1"
    assert "1.0 → 1.1" in out.impact_summary
    assert "tracked_files.<id>.mode" in out.impact_summary
    assert "migrate --apply" in out.impact_summary


def test_assess_schema_change_detected_from_breaking_block() -> None:
    notes = (
        "### Changed\n"
        "- BREAKING: schema field `bootstrap` renamed to `scaffold`.\n"
    )
    out = _assess_schema_change(notes, current_schema="1.0", is_major_bump=False)
    assert out.kind is SchemaChangeKind.DETECTED
    assert "BREAKING" in out.impact_summary
    assert "scaffold" in out.impact_summary


def test_assess_schema_change_unknown_on_major_bump_with_no_signal() -> None:
    notes = "### Added\n- minor docs tweak"
    out = _assess_schema_change(notes, current_schema="1.0", is_major_bump=True)
    assert out.kind is SchemaChangeKind.UNKNOWN
    assert "Major-version bump" in out.impact_summary


def test_assess_schema_change_unknown_when_notes_absent() -> None:
    out = _assess_schema_change(None, current_schema="1.0", is_major_bump=False)
    assert out.kind is SchemaChangeKind.UNKNOWN
    assert "migrate --check" in out.impact_summary


# ---------------------------------------------------------------------------
# Confirm panel rendering
# ---------------------------------------------------------------------------


def _make_plan(
    *,
    schema_kind: SchemaChangeKind = SchemaChangeKind.NONE,
    target: str = "0.3.0",
    current: str = "0.2.0",
    release_notes: str | None = "- body",
    is_major_bump: bool = False,
    breaking_flag: bool = False,
) -> UpgradePlan:
    summaries = {
        SchemaChangeKind.NONE: "No schema change. Fully backwards compatible.",
        SchemaChangeKind.DETECTED: (
            "SCHEMA CHANGE detected: 1.0 → 1.1\n   • renames: bootstrap → scaffold"
        ),
        SchemaChangeKind.UNKNOWN: "Could not parse schema impact from release notes.",
    }
    assessment = SchemaChangeAssessment(
        kind=schema_kind,
        from_schema="1.0",
        to_schema="1.1" if schema_kind is SchemaChangeKind.DETECTED else None,
        impact_summary=summaries[schema_kind],
    )
    return UpgradePlan(
        current_version=current,
        target_version=target,
        release_notes=release_notes,
        is_major_bump=is_major_bump,
        breaking_changes_flagged=breaking_flag,
        schema_change=assessment,
    )


class _FakeDialog:
    """Stand-in for ``radiolist_dialog(...).run()``."""

    def __init__(self, *, return_value: object) -> None:
        self._return_value = return_value
        self.run_calls = 0
        self.last_kwargs: dict[str, Any] | None = None

    def run(self) -> object:
        self.run_calls += 1
        return self._return_value


class _DialogRecorder:
    def __init__(self, fake: _FakeDialog) -> None:
        self.fake = fake
        self.call_count = 0
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> _FakeDialog:
        self.call_count += 1
        self.kwargs.append(kwargs)
        return self.fake


def _patch_radiolist(
    monkeypatch: pytest.MonkeyPatch, *, return_value: object
) -> _DialogRecorder:
    fake = _FakeDialog(return_value=return_value)
    recorder = _DialogRecorder(fake)
    monkeypatch.setattr("setforge.cli.upgrade.radiolist_dialog", recorder)
    return recorder


def test_confirm_panel_renders_schema_impact_for_all_kinds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Panel always shows ``=== schema impact ===`` regardless of kind."""
    for kind in SchemaChangeKind:
        plan = _make_plan(schema_kind=kind)
        _patch_radiolist(monkeypatch, return_value=UpgradeChoice.UPGRADE)
        _confirm_upgrade(plan, yes=False)
        captured = capsys.readouterr().out
        assert "=== schema impact ===" in captured, (
            f"missing schema impact marker for kind={kind.value}"
        )


def test_confirm_panel_no_prompt_picks_migrate_check_on_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(schema_kind=SchemaChangeKind.DETECTED)
    recorder = _patch_radiolist(monkeypatch, return_value=UpgradeChoice.ABORT)
    choice = _confirm_upgrade(plan, yes=True)
    assert choice is UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK
    assert recorder.call_count == 0


def test_confirm_panel_no_prompt_picks_upgrade_on_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(schema_kind=SchemaChangeKind.NONE)
    recorder = _patch_radiolist(monkeypatch, return_value=UpgradeChoice.ABORT)
    choice = _confirm_upgrade(plan, yes=True)
    assert choice is UpgradeChoice.UPGRADE
    assert recorder.call_count == 0


def test_confirm_panel_esc_returns_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _make_plan(schema_kind=SchemaChangeKind.NONE)
    _patch_radiolist(monkeypatch, return_value=None)
    choice = _confirm_upgrade(plan, yes=False)
    assert choice is UpgradeChoice.ABORT


def test_confirm_panel_default_biases_migrate_check_when_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(schema_kind=SchemaChangeKind.DETECTED)
    recorder = _patch_radiolist(monkeypatch, return_value=UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK)
    _confirm_upgrade(plan, yes=False)
    assert recorder.kwargs[0]["default"] is UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK


def test_confirm_panel_default_biases_upgrade_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _make_plan(schema_kind=SchemaChangeKind.NONE)
    recorder = _patch_radiolist(monkeypatch, return_value=UpgradeChoice.UPGRADE)
    _confirm_upgrade(plan, yes=False)
    assert recorder.kwargs[0]["default"] is UpgradeChoice.UPGRADE


# ---------------------------------------------------------------------------
# _build_upgrade_plan integration (PyPI + CHANGELOG mocked at function-level)
# ---------------------------------------------------------------------------


def _patch_pypi(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str,
    is_prerelease: bool = False,
    yanked: bool = False,
) -> None:
    def fake_fetch(**_kwargs: Any) -> PyPIVersionInfo:
        return PyPIVersionInfo(
            version=version,
            is_prerelease=is_prerelease,
            yanked=yanked,
            yanked_reason=None,
        )

    monkeypatch.setattr("setforge.cli.upgrade.fetch_latest_version", fake_fetch)


def _patch_notes(
    monkeypatch: pytest.MonkeyPatch, *, notes: str | None
) -> None:
    monkeypatch.setattr(
        "setforge.cli.upgrade._load_release_notes", lambda _v: notes
    )


def test_build_upgrade_plan_passes_through_pypi_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.5.0")
    _patch_notes(monkeypatch, notes="### Added\n- something")
    plan = _build_upgrade_plan(to=None, prerelease=False)
    assert plan.target_version == "0.5.0"
    assert plan.release_notes is not None
    assert plan.schema_change.kind is SchemaChangeKind.NONE


def test_build_upgrade_plan_to_pins_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.5.0")
    _patch_notes(monkeypatch, notes=None)
    plan = _build_upgrade_plan(to="0.4.2", prerelease=False)
    assert plan.target_version == "0.4.2"
    assert any("--to=0.4.2 pins" in w for w in plan.extra_warnings)


def test_build_upgrade_plan_rejects_invalid_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.5.0")
    _patch_notes(monkeypatch, notes=None)
    from setforge.errors import UpgradeError

    with pytest.raises(UpgradeError, match="not a valid X.Y.Z"):
        _build_upgrade_plan(to="not-a-version", prerelease=False)


# ---------------------------------------------------------------------------
# CLI entry point — --check + full upgrade flow
# ---------------------------------------------------------------------------


def _patch_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: Iterable[subprocess.CompletedProcess[str]],
) -> list[list[str]]:
    """Replace ``subprocess.run`` with a scripted-response generator."""
    calls: list[list[str]] = []
    iterator = iter(responses)

    def fake_run(
        cmd: list[str],
        *,
        capture_output: bool = False,  # noqa: ARG001
        text: bool = False,  # noqa: ARG001
        check: bool = False,  # noqa: ARG001
        timeout: float | None = None,  # noqa: ARG001
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError(f"unexpected subprocess call: {cmd!r}") from exc

    monkeypatch.setattr(upgrade_mod.subprocess, "run", fake_run)
    return calls


def test_cli_upgrade_check_mode_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--check`` must NOT shell out to ``uv tool upgrade``."""
    _patch_pypi(monkeypatch, version="0.3.0")
    _patch_notes(monkeypatch, notes="- body")

    def fail_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("--check must not invoke subprocess.run")

    monkeypatch.setattr(upgrade_mod.subprocess, "run", fail_run)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--check"])
    assert result.exit_code == 0, result.output
    assert "0.3.0" in result.output
    assert "=== schema impact ===" in result.output


def test_cli_upgrade_already_latest_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge import __version__ as current

    _patch_pypi(monkeypatch, version=current)
    _patch_notes(monkeypatch, notes=None)
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert "already on the latest version" in result.output


def test_cli_upgrade_full_flow_no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end with subprocess mocked: pypi → wrap → verify → rollback line."""
    _patch_pypi(monkeypatch, version="0.3.0")
    _patch_notes(monkeypatch, notes="### Added\n- shiny")
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=["uv", "tool", "upgrade", "setforge"],
            returncode=0,
            stdout="Resolved 12 packages in 200ms\nInstalled setforge==0.3.0\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=["uv", "tool", "list"],
            returncode=0,
            stdout="setforge 0.3.0\n",
            stderr="",
        ),
    ]
    calls = _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[0][1:] == ["tool", "upgrade", "setforge"]
    assert calls[1][1:] == ["tool", "list"]
    assert "rollback:" in result.output
    assert "upgraded to 0.3.0" in result.output


def test_cli_upgrade_full_flow_with_migrate_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When schema is DETECTED, --no-prompt auto-runs migrate --check."""
    _patch_pypi(monkeypatch, version="0.3.0")
    _patch_notes(
        monkeypatch,
        notes="### Changed\n- schema_version bumped 1.0 → 1.1 (additive)\n",
    )
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=["uv", "tool", "upgrade", "setforge"],
            returncode=0,
            stdout="Installed setforge==0.3.0\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=["uv", "tool", "list"],
            returncode=0,
            stdout="setforge 0.3.0\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=["uv", "run", "setforge", "migrate", "--check"],
            returncode=0,
            stdout="no migrations pending\n",
            stderr="",
        ),
    ]
    calls = _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 3
    assert calls[2][1:] == ["run", "setforge", "migrate", "--check"]
    assert "no migrations pending" in result.output


def test_cli_upgrade_migrate_check_soft_fails_when_command_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.3.0")
    _patch_notes(
        monkeypatch,
        notes="### Changed\n- schema_version bumped 1.0 → 1.1\n",
    )
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Installed setforge==0.3.0\n", stderr=""
        ),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="setforge 0.3.0\n", stderr=""
        ),
        subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="Error: No such command 'migrate'.",
        ),
    ]
    _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert "is not available" in result.output


def test_cli_upgrade_parses_nothing_to_upgrade_as_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per research brief §2: STDOUT 'Nothing to upgrade' = no-op, exit 0."""
    _patch_pypi(monkeypatch, version="0.3.0")
    _patch_notes(monkeypatch, notes="- body")
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Nothing to upgrade.\n",
            stderr="",
        ),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="setforge 0.3.0\n", stderr=""
        ),
    ]
    _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code == 0, result.output
    assert "no-op" in result.output


def test_cli_upgrade_wrap_failure_surfaces_upgrade_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.3.0")
    _patch_notes(monkeypatch, notes="- body")
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="permission denied"
        ),
    ]
    _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    # The Typer top-level main() handler renders SetforgeError → exit 1; the
    # CliRunner bypasses that handler, so the UpgradeError raises directly.
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code != 0
    assert "permission denied" in (
        str(result.exception) if result.exception else ""
    ) or "permission denied" in result.output


def test_cli_upgrade_post_verify_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pypi(monkeypatch, version="0.3.0")
    _patch_notes(monkeypatch, notes="- body")
    monkeypatch.setattr("setforge.cli.upgrade.shutil.which", lambda _b: "/u/bin/uv")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    responses = [
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Installed setforge==0.3.0\n", stderr=""
        ),
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="setforge 0.2.0\n", stderr=""
        ),
    ]
    _patch_subprocess_run(monkeypatch, responses=responses)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--no-prompt"])
    assert result.exit_code != 0
    excmsg = str(result.exception) if result.exception else result.output
    assert "post-upgrade verification" in excmsg


def test_cli_upgrade_pypi_fetch_error_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.errors import PyPIFetchError

    def boom(**_kwargs: Any) -> PyPIVersionInfo:
        raise PyPIFetchError("no network")

    monkeypatch.setattr("setforge.cli.upgrade.fetch_latest_version", boom)
    runner = CliRunner()
    result = runner.invoke(app, ["upgrade", "--check"])
    assert result.exit_code == 1
    assert "no network" in result.output


# ---------------------------------------------------------------------------
# Wizard discipline guard
# ---------------------------------------------------------------------------


def test_upgrade_module_uses_only_radiolist_no_typer_prompt() -> None:
    """Grep upgrade.py for forbidden prompt shapes."""
    text = (upgrade_mod.__file__ or "")
    assert text  # path must be present
    from pathlib import Path

    source = Path(text).read_text(encoding="utf-8")
    for forbidden in (
        "typer.prompt",
        "typer.confirm",
        "click.prompt",
        "click.confirm",
        "input(",
    ):
        assert forbidden not in source, f"forbidden prompt {forbidden!r} in upgrade.py"
