"""Integration tests for the install-side store-state snapshot barrier.

Drive the real ``setforge install`` CLI (recording transitions) against a
temp config repo with a sandboxed ``$HOME`` + ``$SETFORGE_STATE_DIR`` and
assert the transition's ``state_snapshots/`` payload captures the
PRE-install state of every reconcile-store entry pass 2 can touch — the
byte base, the reconcile local/absent/drafts legs, and the per-profile
index — at the pass-2 barrier, and that the store files no longer ride
``changes.patch``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store, transitions
from setforge.cli import app
from setforge.transitions import SnapshotStore

_PROFILE = "test-snapshots"
_FILE_ID = "doc"

_DOC = """\
# Title

## Notes

Shared body original.
"""


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_snap/doc.md\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, body: str) -> None:
    src = repo / "tracked" / "doc.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(body, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _install(config: Path) -> Result:
    """Run a transition-RECORDING install (no --no-transition)."""
    args = [
        "install",
        f"--profile={_PROFILE}",
        f"--config={config}",
        "--no-secrets-scan",
        "--no-git-check",
        "--yes",
    ]
    return CliRunner().invoke(app, args)


def _latest_snapshots() -> tuple[transitions.StateSnapshotEntry, ...]:
    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    snapshots = transitions.load_state_snapshots(latest)
    assert snapshots is not None
    return snapshots


def test_first_install_records_absent_entries_for_all_reconcile_stores(
    repo: Path,
) -> None:
    """A fresh first install snapshots every reconcile-store entry as ABSENT
    (payload None) — the state revert must restore by DELETING."""
    _write_tracked(repo, _DOC)
    config = _write_config(repo)

    result = _install(config)
    assert result.exit_code == 0, result.output

    snapshots = _latest_snapshots()
    covered = {(e.store, e.key) for e in snapshots}
    assert covered == {
        (SnapshotStore.BASE, _FILE_ID),
        (SnapshotStore.LOCAL_CONTENT, _FILE_ID),
        (SnapshotStore.LOCAL_ABSENT, _FILE_ID),
        (SnapshotStore.DRAFTS, _FILE_ID),
        (SnapshotStore.INDEX, _PROFILE),
    }
    assert all(e.payload is None for e in snapshots)
    assert all(e.profile == _PROFILE for e in snapshots)


def test_second_install_snapshots_pre_install_store_state(repo: Path) -> None:
    """The barrier captures the stores as they stood BEFORE pass-2 writes:
    a second install records the FIRST install's byte base + reconcile legs."""
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    base_v1 = base_store.base_path(_PROFILE, _FILE_ID).read_bytes()

    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body V2."))
    assert _install(config).exit_code == 0

    by_store = {e.store: e for e in _latest_snapshots()}
    # The byte base captured pre-second-install is the first install's base.
    assert by_store[SnapshotStore.BASE].payload == base_v1
    # The reconcile local leg was populated by the first install, so its
    # pre-second-install snapshot is non-absent (the deployed content).
    assert by_store[SnapshotStore.LOCAL_CONTENT].payload is not None
    # The per-profile index likewise pre-existed the second install.
    assert by_store[SnapshotStore.INDEX].payload is not None


def test_store_paths_absent_from_changes_patch(repo: Path) -> None:
    """Store files leave the patch mechanism: their pre/post states ride
    state_snapshots/ exclusively, so revert never double-restores them."""
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    # A second install with an upstream edit advances the byte base —
    # exactly the delta the old mechanism recorded into changes.patch.
    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body V2."))
    assert _install(config).exit_code == 0

    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    patch_file = latest / "changes.patch"
    assert patch_file.exists()
    patch_text = patch_file.read_text(encoding="utf-8")

    base_rel = str(base_store.base_path(_PROFILE, _FILE_ID)).lstrip("/")
    assert base_rel not in patch_text
