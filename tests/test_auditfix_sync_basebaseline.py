"""Regression: ``sync`` records state_snapshots so revert restores in lockstep.

A ``sync --auto=use-live`` that absorbs a live edit rewrites the tracked src.
For a reconcile-managed file it also records the pre-sync per-host store state
(BASE + the reconcile local/absent/drafts legs) into the sync transition's
``state_snapshots/`` payload, so ``setforge revert`` after such a sync restores
BOTH the tracked src AND the byte base to their pre-sync state instead of
leaving the store advanced past the reverted tracked src.

Note the base invariant under the unified reconcile model: sync **does not**
advance the byte base — it advances only on ``install`` when new upstream is
fetched (see :func:`setforge.capture._capture_reconcile_plain`). So the base is
unchanged across the sync AND the revert; the state_snapshot mechanism still
round-trips it (a no-op restore here, but the wiring is what this pins).
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

# Live edit absorbed by sync --auto=use-live: re-baselines tracked.
_DOC_LIVE_EDIT = _DOC.replace("Shared body original.", "MY LIVE EDIT.")


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_syncbase/doc.md\n"
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


def test_sync_revert_restores_tracked_and_base_in_lockstep(repo: Path) -> None:
    """sync absorbs a live edit into tracked; revert restores the tracked src.

    The byte base is untouched by BOTH sync and revert (it advances only on
    install), and the sync transition still records the reconcile store's
    pre-sync state so revert round-trips it in lockstep with the tracked src.
    """
    _write_tracked(repo, _DOC)
    config = _write_config(repo)

    # Install seeds live + the reconcile byte base from tracked.
    assert _install(config).exit_code == 0
    pre_sync_tracked = _tracked_src(repo).read_bytes()
    pre_sync_base = base_store.read_base(_PROFILE, _MD_ID)
    assert pre_sync_base is not None

    # Edit live, then sync --auto=use-live: tracked re-baselines to the live
    # bytes; the byte base is left for the next install to advance.
    _live_md().write_text(_DOC_LIVE_EDIT, encoding="utf-8")
    result = _sync(config)
    assert result.exit_code == 0, result.output
    assert _tracked_src(repo).read_bytes() != pre_sync_tracked
    assert base_store.read_base(_PROFILE, _MD_ID) == pre_sync_base  # base unchanged

    # Revert must restore the tracked src; the base stays put (round-trip no-op).
    result = _revert(config)
    assert result.exit_code == 0, result.output
    assert _tracked_src(repo).read_bytes() == pre_sync_tracked
    assert base_store.read_base(_PROFILE, _MD_ID) == pre_sync_base
