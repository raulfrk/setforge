"""CLI and locked-apply tests for unrecorded orphan scanning."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from setforge import operations, transitions
from setforge.cli import app
from setforge.cli import orphans as orphans_mod
from setforge.errors import OrphanCleanupRequiresInteractive, SetforgeError


def _config(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    tracked = repo / "tracked"
    tracked.mkdir(parents=True)
    (tracked / "kept.txt").write_text("tracked", encoding="utf-8")
    live_root = Path.home() / ".scan-cli"
    live = live_root / "tool"
    live.mkdir(parents=True)
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.0'\n"
        "tracked_files:\n"
        "  kept:\n"
        "    src: kept.txt\n"
        f"    dst: {live / 'kept.txt'}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [kept]\n",
        encoding="utf-8",
    )
    return config, live, live / "candidate"


@pytest.mark.parametrize("extra", [("--yes",), ("--ignore", "old")])
def test_scan_rejects_blanket_or_ignore_before_config_load(
    extra: tuple[str, ...],
) -> None:
    result = CliRunner().invoke(
        app, ["cleanup-orphans", "--scan", "--profile=p", *extra]
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, SetforgeError)


def test_scan_dry_run_surfaces_unrecorded_without_mutation(tmp_path: Path) -> None:
    config, _live, candidate = _config(tmp_path)
    candidate.write_bytes(b"\x00candidate")

    result = CliRunner().invoke(
        app,
        ["cleanup-orphans", "--scan", "--profile=p", f"--config={config}"],
    )

    assert result.exit_code == 0, result.output
    assert "unrecorded managed-tree candidates" in result.output
    assert "REVIEW" in result.output
    assert str(candidate) in result.output.replace("\n", "")
    assert candidate.read_bytes() == b"\x00candidate"


def test_scan_apply_requires_tty(tmp_path: Path) -> None:
    config, _live, candidate = _config(tmp_path)
    candidate.write_text("candidate", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "cleanup-orphans",
            "--scan",
            "--apply",
            "--profile=p",
            f"--config={config}",
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, OrphanCleanupRequiresInteractive)
    assert candidate.exists()


def test_scan_confirmation_is_individual_and_defaults_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, first = _config(tmp_path)
    first.write_text("first", encoding="utf-8")
    second = live / "second"
    second.write_text("second", encoding="utf-8")
    entries = orphans_mod._detect_scan_live("p", config)[1].entries
    decisions = iter((False, True))
    defaults: list[bool] = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def _confirm(*_args: object, default: bool) -> bool:
        defaults.append(default)
        return next(decisions)

    monkeypatch.setattr(orphans_mod.typer, "confirm", _confirm)

    approved = orphans_mod._confirm_scan_entries(entries, Console())

    assert [entry.path for entry in approved] == [second]
    assert defaults == [False, False]


def test_scan_apply_individual_consent_writes_transition_and_clears_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live, candidate = _config(tmp_path)
    candidate.write_bytes(b"\x00\xffcandidate")
    candidate.chmod(0o640)
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(orphans_mod.typer, "confirm", lambda *_a, **_kw: True)

    orphans_mod._execute_scan_cleanup("p", config, console=Console())

    assert not candidate.exists()
    assert candidate.parent.is_dir()
    transition = transitions.load_latest("p")
    assert transition is not None
    assert transitions.load_filesystem_deltas(transition)
    assert operations.active("p") is None

    revert = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert candidate.read_bytes() == b"\x00\xffcandidate"
    assert candidate.stat().st_mode & 0o777 == 0o640
    assert candidate.parent.is_dir()

    redo = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )
    assert redo.exit_code == 0, redo.output
    assert not candidate.exists()
    assert candidate.parent.is_dir()


def test_scan_locked_rescan_can_only_contract_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _live, candidate = _config(tmp_path)
    candidate.write_text("approved", encoding="utf-8")
    cfg, initial = orphans_mod._detect_scan_live("p", config)
    approved = initial.entries[0]
    candidate.unlink()
    candidate.write_text("replacement", encoding="utf-8")
    _, refreshed = orphans_mod._detect_scan_live("p", config)
    detections = iter(((cfg, initial), (cfg, refreshed)))
    monkeypatch.setattr(
        orphans_mod, "_detect_scan_live", lambda *_args: next(detections)
    )
    monkeypatch.setattr(
        orphans_mod, "_confirm_scan_entries", lambda *_args: (approved,)
    )

    orphans_mod._execute_scan_cleanup("p", config, console=Console())

    assert candidate.read_text(encoding="utf-8") == "replacement"
    assert transitions.load_latest("p") is None


def test_scan_locked_rescan_does_not_expand_to_new_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, approved_path = _config(tmp_path)
    approved_path.write_text("approved", encoding="utf-8")
    cfg, initial = orphans_mod._detect_scan_live("p", config)
    newly_seen = live / "new-after-consent"
    newly_seen.write_text("new", encoding="utf-8")
    _, refreshed = orphans_mod._detect_scan_live("p", config)
    detections = iter(((cfg, initial), (cfg, refreshed)))
    monkeypatch.setattr(
        orphans_mod, "_detect_scan_live", lambda *_args: next(detections)
    )
    monkeypatch.setattr(
        orphans_mod, "_confirm_scan_entries", lambda *_args: initial.entries
    )

    orphans_mod._execute_scan_cleanup("p", config, console=Console())

    assert not approved_path.exists()
    assert newly_seen.read_text(encoding="utf-8") == "new"


def test_legacy_cleanup_revert_restores_binary_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, binary = _config(tmp_path)
    binary.write_bytes(b"\x00\xfflegacy")
    binary.chmod(0o640)
    target = tmp_path / "important"
    target.write_text("keep", encoding="utf-8")
    link = live / "old-link"
    link.symlink_to(target)
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    record = state / "transitions" / "20260101T000000000000Z-install-p"
    record.mkdir(parents=True)
    (record / "meta.json").write_text(
        json.dumps(
            {
                "command": "install",
                "profile": "p",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "host": "test",
                "version": "0.3.0",
                "paths": [str(binary), str(link)],
            }
        ),
        encoding="utf-8",
    )

    cleanup = CliRunner().invoke(
        app,
        [
            "cleanup-orphans",
            "--apply",
            "--yes",
            "--profile=p",
            f"--config={config}",
        ],
    )
    assert cleanup.exit_code == 0, cleanup.output
    assert not binary.exists()
    assert not link.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"

    revert = CliRunner().invoke(
        app, ["revert", "--profile=p", f"--config={config}", "--yes"]
    )
    assert revert.exit_code == 0, revert.output
    assert binary.read_bytes() == b"\x00\xfflegacy"
    assert binary.stat().st_mode & 0o777 == 0o640
    assert link.is_symlink()
    assert link.readlink() == target


def test_scan_partial_delete_failure_restores_all_and_removes_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, first = _config(tmp_path)
    first.write_bytes(b"first")
    second = live / "second"
    second.write_bytes(b"second")
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        orphans_mod, "_confirm_scan_entries", lambda entries, _console: entries
    )
    real_unlink = orphans_mod.orphan_scan.unlink_approved_entry
    calls = 0

    def _fail_second(entry) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second unlink failure")
        real_unlink(entry)

    monkeypatch.setattr(orphans_mod.orphan_scan, "unlink_approved_entry", _fail_second)

    with pytest.raises(OSError, match="injected second unlink failure") as caught:
        orphans_mod._execute_scan_cleanup("p", config, console=Console())

    assert getattr(caught.value, "__notes__", []) == []
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert transitions.load_latest("p") is None
    assert operations.active("p") is None


def test_scan_recovery_preserves_replacement_for_unpublished_later_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, first = _config(tmp_path)
    first.write_bytes(b"first")
    second = live / "second"
    second.write_bytes(b"second-original")
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        orphans_mod, "_confirm_scan_entries", lambda entries, _console: entries
    )
    real_unlink = orphans_mod.orphan_scan.unlink_approved_entry
    calls = 0

    def _replace_before_second_validation(entry) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            entry.path.unlink()
            entry.path.write_bytes(b"external-replacement")
        real_unlink(entry)

    monkeypatch.setattr(
        orphans_mod.orphan_scan,
        "unlink_approved_entry",
        _replace_before_second_validation,
    )

    with pytest.raises(SetforgeError, match="scan candidate changed"):
        orphans_mod._execute_scan_cleanup("p", config, console=Console())

    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"external-replacement"
    assert transitions.load_latest("p") is not None
    active = operations.active("p")
    assert active is not None
    assert active.phase is operations.OperationPhase.RECOVERING
    second_snapshot = next(item for item in active.paths if item.path == second)
    assert second_snapshot.payload == b"second-original"


def test_scan_recovery_retains_original_when_deleted_leaf_is_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, first = _config(tmp_path)
    first.write_bytes(b"first-original")
    second = live / "second"
    second.write_bytes(b"second-original")
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        orphans_mod, "_confirm_scan_entries", lambda entries, _console: entries
    )
    real_unlink = orphans_mod.orphan_scan.unlink_approved_entry
    calls = 0

    def _recreate_then_fail(entry) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_unlink(entry)
            entry.path.write_bytes(b"external-replacement")
            return
        raise OSError("injected second unlink failure")

    monkeypatch.setattr(
        orphans_mod.orphan_scan, "unlink_approved_entry", _recreate_then_fail
    )

    with pytest.raises(OSError, match="injected second unlink failure") as caught:
        orphans_mod._execute_scan_cleanup("p", config, console=Console())

    assert first.read_bytes() == b"external-replacement"
    assert second.read_bytes() == b"second-original"
    assert transitions.load_latest("p") is not None
    active = operations.active("p")
    assert active is not None
    assert active.phase is operations.OperationPhase.RECOVERING
    first_snapshot = next(item for item in active.paths if item.path == first)
    assert first_snapshot.payload == b"first-original"
    assert any("journal was retained" in note for note in caught.value.__notes__)


def test_scan_recovery_preserves_replacement_created_after_path_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, live, first = _config(tmp_path)
    first.write_bytes(b"first-original")
    second = live / "second"
    second.write_bytes(b"second-original")
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        orphans_mod, "_confirm_scan_entries", lambda entries, _console: entries
    )
    real_unlink = orphans_mod.orphan_scan.unlink_approved_entry
    unlink_calls = 0

    def _fail_second(entry) -> None:
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls == 2:
            raise OSError("injected second unlink failure")
        real_unlink(entry)

    monkeypatch.setattr(orphans_mod.orphan_scan, "unlink_approved_entry", _fail_second)
    real_restore = operations._restore_path
    raced = False

    def _race_restore(
        snapshot: operations.PathSnapshot,
        *,
        guard_identities: dict[Path, tuple[int, int, int] | None] | None = None,
        permit_existing_absent: bool = False,
        require_leaf_absent: bool = False,
    ) -> bool:
        nonlocal raced
        if snapshot.path == first and not raced:
            raced = True
            first.write_bytes(b"late-external-replacement")
        return real_restore(
            snapshot,
            guard_identities=guard_identities,
            permit_existing_absent=permit_existing_absent,
            require_leaf_absent=require_leaf_absent,
        )

    monkeypatch.setattr(operations, "_restore_path", _race_restore)

    with pytest.raises(OSError, match="injected second unlink failure"):
        orphans_mod._execute_scan_cleanup("p", config, console=Console())

    assert raced
    assert first.read_bytes() == b"late-external-replacement"
    assert transitions.load_latest("p") is not None
    assert operations.active("p") is not None
