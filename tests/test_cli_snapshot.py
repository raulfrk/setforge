"""CLI integration tests for ``setforge snapshot``.

Exercises the typer surface via :class:`typer.testing.CliRunner` against
a fixture config + fixture profile, with ``Path.home`` monkeypatched
into a per-test tmp dir so snapshot writes land in the sandbox.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import Result
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import operations
from setforge import snapshots as snap_mod
from setforge.cli import app
from setforge.cli import snapshot as cli_snap


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox ``Path.home()`` + the snapshot module's local.yaml constant."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    fake_local = tmp_path / ".config" / "setforge" / "local.yaml"
    monkeypatch.setattr(snap_mod, "LOCAL_CONFIG_PATH", fake_local)
    return tmp_path


@pytest.fixture
def config_repo(tmp_path: Path) -> Path:
    """Lay down a minimal setforge.yaml + tracked/ tree in a separate repo dir."""
    repo = tmp_path / "config-repo"
    (repo / "tracked" / "claude").mkdir(parents=True)
    (repo / "tracked" / "claude" / "CLAUDE.md").write_text("# Tracked CLAUDE.md body\n")
    (repo / "tracked" / "claude" / "SECOND.md").write_text("# Second body\n")
    cfg = {
        "version": 1,
        "schema_version": "1.0",
        "tracked_files": {
            "claude_md": {
                "src": "claude/CLAUDE.md",
                "dst": str(tmp_path / "live" / "CLAUDE.md"),
            },
            "second_md": {
                "src": "claude/SECOND.md",
                "dst": str(tmp_path / "live" / "SECOND.md"),
            },
        },
        "profiles": {
            "test-profile": {
                "tracked_files": ["claude_md", "second_md"],
            },
            "other-profile": {
                "tracked_files": ["claude_md", "second_md"],
            },
        },
    }
    buf = io.StringIO()
    YAML(typ="safe").dump(cfg, buf)
    config_path = repo / "setforge.yaml"
    config_path.write_text(buf.getvalue())
    return config_path


def _invoke(args: Iterable[str]) -> Result:
    """Run the setforge CLI with ``CliRunner``; return the typer Result.

    ``CliRunner.invoke`` does NOT run ``setforge.cli.main()`` — it runs
    ``app()`` directly — so :class:`SetforgeError` propagates as
    ``result.exception`` rather than being rendered + exit-1'd by the
    main wrapper. The helper below normalizes by inspecting
    ``result.exception`` and reporting exit_code=1 when a
    SetforgeError surfaced; tests then assert against str(exception).
    """
    runner = CliRunner()
    return runner.invoke(app, list(args))


def _outerr(result: Result) -> str:
    """Combine stdout + any SetforgeError message into one string for asserts."""
    parts: list[str] = [result.stdout or ""]
    exc = result.exception
    if exc is not None:
        parts.append(str(exc))
    return "\n".join(parts)


def _effective_exit_code(result: Result) -> int:
    """Return ``exit_code`` collapsing the SetforgeError-as-exit-1 contract."""
    exc = result.exception
    if exc is not None:
        from setforge.errors import SetforgeError

        if isinstance(exc, SetforgeError):
            return 1
    return result.exit_code


def _seed_live_file(home: Path) -> Path:
    """Drop a live destination file matching the fixture's tracked_files.dst."""
    dst = home / "live" / "CLAUDE.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("live body\n")
    return dst


def _seed_second_live_file(home: Path) -> Path:
    dst = home / "live" / "SECOND.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("second live body\n")
    return dst


def test_snapshot_create_writes_meta_and_file(
    fake_home: Path, config_repo: Path
) -> None:
    """``snapshot create`` writes a finalized snapshot dir + emits success banner."""
    dst = _seed_live_file(fake_home)
    result = _invoke(
        [
            "snapshot",
            "create",
            "first",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert result.exit_code == 0, result.output
    snaps = snap_mod.list_snapshots()
    assert len(snaps) == 1
    assert snaps[0].label == "first"
    assert dst in snaps[0].files


def test_snapshot_create_rejects_negative_keep(
    fake_home: Path, config_repo: Path
) -> None:
    _seed_live_file(fake_home)
    result = _invoke(
        [
            "snapshot",
            "create",
            "neg",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--keep=-1",
        ]
    )
    assert _effective_exit_code(result) == 1
    assert "non-negative" in _outerr(result)


def test_snapshot_list_empty_emits_hint(fake_home: Path) -> None:
    result = _invoke(["snapshot", "list"])
    assert result.exit_code == 0
    assert "no snapshots yet" in result.stdout


def test_snapshot_list_shows_newest_first(fake_home: Path, config_repo: Path) -> None:
    _seed_live_file(fake_home)
    for label in ("alpha", "beta", "gamma"):
        result = _invoke(
            [
                "snapshot",
                "create",
                label,
                "--profile=test-profile",
                f"--config={config_repo}",
            ]
        )
        assert result.exit_code == 0
    result = _invoke(["snapshot", "list"])
    assert result.exit_code == 0
    # rich.Table renders into stdout; the most recent label appears
    # before the older ones (lexicographic on timestamp prefix => DESC).
    pos_gamma = result.stdout.find("gamma")
    pos_beta = result.stdout.find("beta")
    pos_alpha = result.stdout.find("alpha")
    assert pos_gamma >= 0
    assert pos_beta >= 0
    assert pos_alpha >= 0
    assert pos_gamma < pos_beta < pos_alpha


def test_snapshot_restore_yes_overlays_files(
    fake_home: Path, config_repo: Path
) -> None:
    """``--yes`` bypasses the wizard and applies an additive overlay."""
    dst = _seed_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "saved",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0
    # Drift the live file AFTER snapshot.
    dst.write_text("drifted body\n")
    # Add a sibling that wasn't in the snapshot — must be left alone.
    sibling = dst.parent / "live-only.md"
    sibling.write_text("sibling body\n")
    result = _invoke(
        [
            "snapshot",
            "restore",
            "saved",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )
    assert result.exit_code == 0, result.output
    assert dst.read_text() == "live body\n", "additive overlay restored snapshot body"
    assert sibling.read_text() == "sibling body\n", "live-only file untouched"


def test_snapshot_restore_recreates_deleted_destination_tree(
    fake_home: Path, config_repo: Path
) -> None:
    dst = _seed_live_file(fake_home)
    _seed_second_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "deleted-tree",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0, create.output
    shutil.rmtree(dst.parent)

    result = _invoke(
        [
            "snapshot",
            "restore",
            "deleted-tree",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )

    assert result.exit_code == 0, _outerr(result)
    assert dst.read_text() == "live body\n"


def test_snapshot_restore_rejects_snapshot_from_another_profile(
    fake_home: Path, config_repo: Path
) -> None:
    dst = _seed_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "owned",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0, create.output
    dst.write_text("must remain drifted\n")

    result = _invoke(
        [
            "snapshot",
            "restore",
            "owned",
            "--profile=other-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )

    assert _effective_exit_code(result) == 1
    assert "profile" in _outerr(result)
    assert dst.read_text() == "must remain drifted\n"


def test_snapshot_restore_preflights_every_mirror_before_first_write(
    fake_home: Path, config_repo: Path
) -> None:
    first = _seed_live_file(fake_home)
    second = _seed_second_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "two-files",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0, create.output
    meta = snap_mod.resolve_snapshot("two-files")
    broken_mirror = (
        snap_mod.snapshots_root() / meta.snapshot_id / second.relative_to("/")
    )
    broken_mirror.unlink()
    first.write_text("first drift\n")
    second.write_text("second drift\n")

    result = _invoke(
        [
            "snapshot",
            "restore",
            "two-files",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )

    assert _effective_exit_code(result) == 1
    assert "missing on disk" in _outerr(result)
    assert first.read_text() == "first drift\n"
    assert second.read_text() == "second drift\n"


def test_snapshot_restore_rolls_back_a_mid_apply_failure(
    fake_home: Path,
    config_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _seed_live_file(fake_home)
    second = _seed_second_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "rollback",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0, create.output
    first.write_text("first before failed restore\n")
    first.chmod(0o640)
    second.write_text("second before failed restore\n")
    first_mtime_ns = 1_700_000_000_111_111_111
    second_mtime_ns = 1_700_000_000_222_222_222
    os.utime(first, ns=(first_mtime_ns, first_mtime_ns))
    os.utime(second, ns=(second_mtime_ns, second_mtime_ns))
    real_write = snap_mod._write_restored_file
    live_writes = 0

    def fail_second_live_write(
        source: snap_mod._FrozenSnapshotFile,
        guard_identities: dict[Path, tuple[int, int, int] | None],
    ) -> None:
        nonlocal live_writes
        destination = source.path
        if destination in {first, second}:
            live_writes += 1
            if live_writes == 2:
                raise OSError("simulated second restore write failure")
        real_write(source, guard_identities)

    monkeypatch.setattr(snap_mod, "_write_restored_file", fail_second_live_write)

    result = _invoke(
        [
            "snapshot",
            "restore",
            "rollback",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, OSError)
    assert live_writes == 2
    assert first.read_text() == "first before failed restore\n"
    assert stat.S_IMODE(first.stat().st_mode) == 0o640
    assert first.stat().st_mtime_ns == first_mtime_ns
    assert second.read_text() == "second before failed restore\n"
    assert second.stat().st_mtime_ns == second_mtime_ns
    assert operations.active("test-profile") is None


def test_snapshot_restore_preserves_preflight_error_when_cleanup_fails(
    fake_home: Path,
    config_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_live_file(fake_home)
    _seed_second_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "cleanup-error",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0, create.output
    primary = RuntimeError("second preflight failed")
    real_validate = snap_mod._validate_restore_plan
    validations = 0

    def fail_second_validation(plan: snap_mod._RestorePlan) -> None:
        nonlocal validations
        validations += 1
        if validations == 2:
            raise primary
        real_validate(plan)

    monkeypatch.setattr(snap_mod, "_validate_restore_plan", fail_second_validation)
    monkeypatch.setattr(
        operations,
        "complete",
        lambda _journal: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    result = _invoke(
        [
            "snapshot",
            "restore",
            "cleanup-error",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )

    assert result.exception is primary
    assert any("cleanup failed" in note for note in primary.__notes__)


def test_snapshot_restore_rejects_destination_removed_from_effective_profile(
    fake_home: Path, config_repo: Path
) -> None:
    first = _seed_live_file(fake_home)
    _seed_second_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "stale-destination",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0, create.output
    first.write_text("must survive\n")
    yaml = YAML(typ="safe")
    cfg = yaml.load(config_repo.read_text(encoding="utf-8"))
    cfg["profiles"]["test-profile"]["tracked_files"] = ["second_md"]
    buf = io.StringIO()
    yaml.dump(cfg, buf)
    config_repo.write_text(buf.getvalue(), encoding="utf-8")

    result = _invoke(
        [
            "snapshot",
            "restore",
            "stale-destination",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )

    assert _effective_exit_code(result) == 1
    assert "no longer managed" in _outerr(result)
    assert first.read_text() == "must survive\n"


def test_snapshot_restore_non_interactive_no_tty_required(
    fake_home: Path, config_repo: Path
) -> None:
    """``--non-interactive`` is a synonym of ``--yes``."""
    _seed_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "ni",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0
    result = _invoke(
        [
            "snapshot",
            "restore",
            "ni",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--non-interactive",
        ]
    )
    assert result.exit_code == 0


def test_snapshot_restore_missing_label_exits_1(
    fake_home: Path, config_repo: Path
) -> None:
    result = _invoke(
        [
            "snapshot",
            "restore",
            "no-such-label",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )
    assert _effective_exit_code(result) == 1
    assert "not found" in _outerr(result)


def test_snapshot_restore_unknown_profile_exits_1(
    fake_home: Path, config_repo: Path
) -> None:
    """Profile lookup failures bubble through the SetforgeError handler."""
    _seed_live_file(fake_home)
    result = _invoke(
        [
            "snapshot",
            "create",
            "bad",
            "--profile=does-not-exist",
            f"--config={config_repo}",
        ]
    )
    assert _effective_exit_code(result) == 1
    assert "profile not found" in _outerr(result)


def test_snapshot_restore_choice_abort_via_button_bar(
    fake_home: Path,
    config_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated interactive run where the user picks ABORT exits 1."""
    _seed_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "cancel-me",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0

    monkeypatch.setattr(
        cli_snap,
        "button_bar",
        lambda *_a, **_kw: cli_snap.RestoreChoice.ABORT,
    )
    monkeypatch.setattr(cli_snap, "_stdin_is_tty", lambda: True)
    result = _invoke(
        [
            "snapshot",
            "restore",
            "cancel-me",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert _effective_exit_code(result) == 1
    assert "aborted" in _outerr(result)


def test_snapshot_restore_choice_cancel_via_button_bar(
    fake_home: Path,
    config_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from setforge.ui.widgets import CANCEL

    _seed_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "esc-me",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0

    monkeypatch.setattr(cli_snap, "button_bar", lambda *_a, **_kw: CANCEL)
    monkeypatch.setattr(cli_snap, "_stdin_is_tty", lambda: True)
    result = _invoke(
        [
            "snapshot",
            "restore",
            "esc-me",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert _effective_exit_code(result) == 1
    assert "aborted" in _outerr(result)


def test_snapshot_restore_refuses_metadata_change_after_confirmation(
    fake_home: Path,
    config_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _seed_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "confirmed",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0
    created = snap_mod.resolve_snapshot("confirmed")
    live.write_text("must survive\n")

    def confirm_then_change(meta: snap_mod.SnapshotMeta, **_kwargs: object):
        meta_path = snap_mod.snapshots_root() / meta.snapshot_id / "_meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["label"] = "changed-after-confirmation"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        return cli_snap.RestoreChoice.RESTORE

    monkeypatch.setattr(cli_snap, "_prompt_restore_choice", confirm_then_change)
    result = _invoke(
        [
            "snapshot",
            "restore",
            created.snapshot_id,
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )

    assert _effective_exit_code(result) == 1
    assert "selection changed after confirmation" in _outerr(result)
    assert live.read_text() == "must survive\n"


def test_snapshot_restore_prewrite_topology_failure_clears_no_effect_journal(
    fake_home: Path,
    config_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _seed_live_file(fake_home)
    _seed_second_live_file(fake_home)
    create = _invoke(
        [
            "snapshot",
            "create",
            "topology-race",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert create.exit_code == 0
    live.write_text("must survive\n")
    real_validate = snap_mod._validate_restore_plan
    calls = 0

    def fail_after_publication(plan: snap_mod._RestorePlan) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SetforgeError("topology changed after journal publication")
        real_validate(plan)

    from setforge.errors import SetforgeError

    monkeypatch.setattr(snap_mod, "_validate_restore_plan", fail_after_publication)
    result = _invoke(
        [
            "snapshot",
            "restore",
            "topology-race",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )

    assert _effective_exit_code(result) == 1
    assert calls == 2
    assert live.read_text() == "must survive\n"
    assert operations.active("test-profile") is None


def test_snapshot_restore_rejects_operation_journal_destination(
    fake_home: Path, config_repo: Path
) -> None:
    yaml = YAML(typ="safe")
    cfg = yaml.load(config_repo.read_text(encoding="utf-8"))
    destination = operations.journal_path("test-profile")
    cfg["tracked_files"]["claude_md"]["dst"] = str(destination)
    cfg["profiles"]["test-profile"]["tracked_files"] = ["claude_md"]
    buf = io.StringIO()
    yaml.dump(cfg, buf)
    config_repo.write_text(buf.getvalue(), encoding="utf-8")
    snapshot_id = "20260819T120000Z-journal-alias"
    snapshot_dir = snap_mod.snapshots_root() / snapshot_id
    mirror = snapshot_dir / destination.relative_to("/")
    mirror.parent.mkdir(parents=True)
    mirror.write_text("captured payload\n", encoding="utf-8")
    snap_mod._write_meta(
        snapshot_dir,
        snap_mod.SnapshotMeta(
            snapshot_id=snapshot_id,
            label="journal-alias",
            created_at=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
            profile="test-profile",
            files=(destination,),
        ),
    )

    result = _invoke(
        [
            "snapshot",
            "restore",
            "journal-alias",
            "--profile=test-profile",
            f"--config={config_repo}",
            "--yes",
        ]
    )

    assert _effective_exit_code(result) == 1
    assert "overlaps SetForge control path" in _outerr(result)
    assert not destination.exists()


def test_snapshot_restore_choice_pre_snapshot_first(
    fake_home: Path,
    config_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RESTORE_WITH_PRE_SNAPSHOT path writes a fresh pre-restore snapshot."""
    dst = _seed_live_file(fake_home)
    _invoke(
        [
            "snapshot",
            "create",
            "v1",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    dst.write_text("drifted v2 body\n")

    monkeypatch.setattr(
        cli_snap,
        "button_bar",
        lambda *_, **__: cli_snap.RestoreChoice.RESTORE_WITH_PRE_SNAPSHOT,
    )
    monkeypatch.setattr(cli_snap, "_stdin_is_tty", lambda: True)

    result = _invoke(
        [
            "snapshot",
            "restore",
            "v1",
            "--profile=test-profile",
            f"--config={config_repo}",
        ]
    )
    assert result.exit_code == 0
    labels = [s.label for s in snap_mod.list_snapshots()]
    assert any(label.startswith("pre-restore-") for label in labels)
    assert dst.read_text() == "live body\n"
