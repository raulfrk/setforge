"""Audit-fix regression: ``--auto=keep-tracked`` must REFUSE capture drift.

The documented contract is that ``capture``/``sync --auto=keep-tracked``
refuses to absorb any drift — the tracked source (and, for a SHARED
disposition, the stored base) are left exactly as authored. Before the fix
both writeback paths ignored ``auto`` for wholesale live→tracked content and
silently overwrote tracked (re-baselining the base for SHARED), the exact
opposite of the contract.

Covered here:

1. SHARED disposition via the real ``sync --auto=keep-tracked`` CLI — tracked
   src AND ``base_store`` are unchanged after a divergent live edit.
2. ``disposition=None`` tracked_file via a direct ``capture_tracked_file``
   call — the live edit is refused, tracked stays as authored.
3. No-drift case stays a NOOP (keep-tracked does not spuriously skip).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store
from setforge.capture import CaptureAction, CaptureAuto, capture_tracked_file
from setforge.cli import app

_PROFILE = "test-keep-tracked"
_FILE_ID = "shared_text"


def _write_disposition_config(repo: Path, *, disposition: str = "shared") -> Path:
    """Write a setforge.yaml whose ``shared_text`` file carries ``disposition``."""
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  shared_text:\n"
        "    src: text/note.txt\n"
        "    dst: ~/.setforge_kt/note.txt\n"
        f"    disposition: {disposition}\n"
        "  anchor:\n"
        "    src: text/anchor.txt\n"
        "    dst: ~/.setforge_kt/anchor.txt\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - shared_text\n"
        "      - anchor\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> Path:
    """Write tracked source bodies; return the ``shared_text`` src path."""
    src = repo / "tracked" / "text" / "note.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")
    (src.parent / "anchor.txt").write_text("anchor\n", encoding="utf-8")
    return src


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp config repo with sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


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
            "--no-transition",
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
            "--no-transition",
            "--auto=keep-tracked",
            "--yes",
        ],
    )


def test_shared_keep_tracked_refuses_and_leaves_base(repo: Path) -> None:
    """shared + keep-tracked: tracked src AND base untouched despite live drift."""
    tracked_body = "line1\nline2\n"
    src = _write_tracked(repo, tracked_body)
    config = _write_disposition_config(repo, disposition="shared")
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


def test_disposition_none_keep_tracked_refuses_writeback(tmp_path: Path) -> None:
    """disposition=None + keep-tracked: divergent live is not captured."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("tracked-original\n", encoding="utf-8")
    dst.write_text("live-DIVERGED\n", encoding="utf-8")

    result = capture_tracked_file(src, dst, auto=CaptureAuto.KEEP_TRACKED)

    assert result.action is CaptureAction.SKIPPED
    assert result.reason == "keep-tracked"
    # Tracked left as authored.
    assert src.read_text(encoding="utf-8") == "tracked-original\n"


def test_keep_tracked_noop_when_no_drift(tmp_path: Path) -> None:
    """keep-tracked does not spuriously skip when tracked already matches live."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("same\n", encoding="utf-8")
    dst.write_text("same\n", encoding="utf-8")

    result = capture_tracked_file(src, dst, auto=CaptureAuto.KEEP_TRACKED)

    assert result.action is CaptureAction.NOOP
