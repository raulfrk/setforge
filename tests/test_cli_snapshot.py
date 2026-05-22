"""CLI integration tests for ``setforge snapshot`` (setforge-of3a).

Exercises the typer surface via :class:`typer.testing.CliRunner` against
a fixture config + fixture profile, with ``Path.home`` monkeypatched
into a per-test tmp dir so snapshot writes land in the sandbox.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from pathlib import Path

import pytest
from click.testing import Result
from ruamel.yaml import YAML
from typer.testing import CliRunner

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
    cfg = {
        "version": 1,
        "schema_version": "1.0",
        "tracked_files": {
            "claude_md": {
                "src": "claude/CLAUDE.md",
                "dst": str(tmp_path / "live" / "CLAUDE.md"),
            },
        },
        "profiles": {
            "test-profile": {
                "tracked_files": ["claude_md"],
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


def test_snapshot_restore_choice_abort_via_radiolist(
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

    class _FakeDialog:
        def __init__(self, _choice: object) -> None:
            self._choice = _choice

        def run(self) -> object:
            return self._choice

    def fake_radiolist(*_a: object, **_kw: object) -> _FakeDialog:
        return _FakeDialog(cli_snap.RestoreChoice.ABORT)

    monkeypatch.setattr(cli_snap, "radiolist_dialog", fake_radiolist)
    # Pretend stdin is a TTY so the prompt path is reached.
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

    class _FakeDialog:
        def run(self) -> object:
            return cli_snap.RestoreChoice.RESTORE_WITH_PRE_SNAPSHOT

    monkeypatch.setattr(cli_snap, "radiolist_dialog", lambda *_, **__: _FakeDialog())
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
