"""Regression: ``sync`` must record state_snapshots so revert restores the base.

``capture`` re-baselines a SHARED disposition file's byte base
(``base_store.write_base``) on every sync that absorbs a live edit. Before
the fix, ``sync`` wrote a transition with ONLY file_pre/file_post and NO
state_snapshots, so ``setforge revert`` after such a sync reversed the
tracked src via ``patch -R`` but left the base store advanced to the
post-sync bytes — base AHEAD of tracked, the corruption direction the
codebase repeatedly guards against ("base ahead of live is corruption").

This test drives a real install → live-edit → ``sync --auto=use-live`` →
``revert`` cycle and asserts BOTH the tracked src AND the byte base are
restored to their pre-sync state. The base assertion is the one that
failed against the old behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store
from setforge.cli import app

_PROFILE = "test-sync-base"
_MD_ID = "doc"

_DOC = """\
# Title

Shared body original.
"""

# Live edit absorbed by sync --auto=use-live: re-baselines tracked + base.
_DOC_LIVE_EDIT = _DOC.replace("Shared body original.", "MY LIVE EDIT.")


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_syncbase/doc.md\n"
        "    disposition: shared\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, md_body: str) -> None:
    tracked = repo / "tracked"
    tracked.mkdir(parents=True, exist_ok=True)
    (tracked / "doc.md").write_text(md_body, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _tracked_src(repo: Path) -> Path:
    return repo / "tracked" / "doc.md"


def _live_md() -> Path:
    return Path.home() / ".setforge_syncbase" / "doc.md"


def _install(config: Path) -> Result:
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    return CliRunner().invoke(app, args)


def _sync(config: Path) -> Result:
    args = [
        "sync",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--auto=use-live",
        "--yes",
    ]
    return CliRunner().invoke(app, args)


def _revert(config: Path) -> Result:
    args = ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"]
    return CliRunner().invoke(app, args)


def test_sync_revert_restores_base_in_lockstep_with_tracked(repo: Path) -> None:
    """sync absorbs a live edit (re-baselining tracked + base); revert
    must restore BOTH the tracked src AND the byte base to pre-sync state.

    Before the fix, revert restored the tracked src but left the base
    advanced to the post-sync (live-edit) bytes — base AHEAD of tracked.
    """
    _write_tracked(repo, _DOC)
    config = _write_config(repo)

    # Install seeds live + the SHARED disposition base from tracked.
    assert _install(config).exit_code == 0
    pre_sync_tracked = _tracked_src(repo).read_bytes()
    pre_sync_base = base_store.read_base(_PROFILE, _MD_ID)
    assert pre_sync_base is not None

    # Edit live, then sync --auto=use-live: tracked + base re-baseline to
    # the live bytes.
    _live_md().write_text(_DOC_LIVE_EDIT, encoding="utf-8")
    result = _sync(config)
    assert result.exit_code == 0, result.output
    assert _tracked_src(repo).read_bytes() != pre_sync_tracked
    base_after_sync = base_store.read_base(_PROFILE, _MD_ID)
    assert base_after_sync is not None
    assert base_after_sync != pre_sync_base  # re-baselined to live

    # Revert must restore tracked AND the base in lockstep.
    result = _revert(config)
    assert result.exit_code == 0, result.output
    assert _tracked_src(repo).read_bytes() == pre_sync_tracked
    # The base assertion is the one that failed against the old behavior:
    # without state_snapshots on the SYNC transition the base stayed at
    # base_after_sync (AHEAD of the reverted tracked src).
    assert base_store.read_base(_PROFILE, _MD_ID) == pre_sync_base
