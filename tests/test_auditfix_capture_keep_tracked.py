"""Audit-fix regression: ``--auto=keep-tracked`` must REFUSE capture drift.

The documented contract is that ``sync --auto=keep-tracked``
refuses to absorb any drift — the tracked source (and, for a SHARED
disposition, the stored base) are left exactly as authored. Before the fix
both writeback paths ignored ``auto`` for wholesale live→tracked content and
silently overwrote tracked (re-baselining the base for SHARED), the exact
opposite of the contract.

Covered here: a SHARED disposition via the real ``sync --auto=keep-tracked``
CLI — tracked src AND ``base_store`` are unchanged after a divergent live edit.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store
from setforge.cli import app
from tests.shared_fixtures import ConfigRepo

_PROFILE = "test-keep-tracked"
_FILE_ID = "shared_text"


def _write_disposition_config(
    config_repo: ConfigRepo, *, disposition: str = "shared"
) -> Path:
    """Write a setforge.yaml whose ``shared_text`` file carries ``disposition``."""
    return config_repo.write_config(
        profile=_PROFILE,
        tracked_files={
            "shared_text": {
                "src": "text/note.txt",
                "dst": "~/.setforge_kt/note.txt",
                "disposition": disposition,
            },
            "anchor": {"src": "text/anchor.txt", "dst": "~/.setforge_kt/anchor.txt"},
        },
    )


def _write_tracked(config_repo: ConfigRepo, body: str) -> Path:
    """Write tracked source bodies; return the ``shared_text`` src path."""
    src = config_repo.write_tracked("text/note.txt", body)
    config_repo.write_tracked("text/anchor.txt", "anchor\n")
    return src


def _live_path() -> Path:
    """Resolve the sandboxed live destination path."""
    return Path.home() / ".setforge_kt" / "note.txt"


def _install(config: Path) -> Result:
    """Run ``setforge install`` against ``config``; return the CliRunner result."""
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
        ],
    )


def _sync_keep_tracked(config: Path) -> Result:
    """Run ``setforge sync --auto=keep-tracked``; return the CliRunner result."""
    return CliRunner().invoke(
        app,
        [
            "sync",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--auto=keep-tracked",
            "--yes",
        ],
    )


def test_shared_keep_tracked_refuses_and_leaves_base(config_repo: ConfigRepo) -> None:
    """shared + keep-tracked: tracked src AND base untouched despite live drift."""
    tracked_body = "line1\nline2\n"
    src = _write_tracked(config_repo, tracked_body)
    config = _write_disposition_config(config_repo, disposition="shared")
    assert _install(config).exit_code == 0
    base_before = base_store.read_base(_PROFILE, _FILE_ID)
    assert base_before == tracked_body.encode("utf-8")

    # Live diverges from tracked.
    _live_path().write_text("line1\nline2\nline3-LIVE\n", encoding="utf-8")

    result = _sync_keep_tracked(config)
    assert result.exit_code == 0, result.output
    # Contract: tracked stays exactly as authored — drift refused.
    assert src.read_text(encoding="utf-8") == tracked_body
    # Contract: base NOT re-baselined to the live bytes.
    assert base_store.read_base(_PROFILE, _FILE_ID) == base_before
