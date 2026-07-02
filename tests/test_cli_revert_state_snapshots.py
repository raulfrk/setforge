"""Integration tests for store-state restore on ``setforge revert``.

Drive real ``install`` / ``revert`` CLI invocations (recording
transitions) against a temp config repo with a sandboxed ``$HOME`` +
``$SETFORGE_STATE_DIR`` and assert the revert side of the snapshot
mechanism:

- pre-snapshot transitions (no ``state_snapshots/`` dir) revert exactly
  as before — stores untouched, no crash
- revert→revert acts as redo: store state round-trips both ways
- an empty (zero-byte) store entry restores as empty, never as deleted
- ``--to-before`` walks the chain newest-first, landing on the oldest
  requested pre-state
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store, transitions
from setforge.cli import app

_PROFILE = "test-snapshots"
_MD_ID = "doc"

_DOC = """\
# Title

## Forked

Forked body original.

## Shared

Shared body original.
"""

_YAML_DOC = "editor:\n  fontSize: 12\n  tabSize: 4\nshared:\n  theme: dark\n"


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        "    dst: ~/.setforge_snap/doc.md\n"
        "  settings:\n"
        "    src: doc.yaml\n"
        "    dst: ~/.setforge_snap/doc.yaml\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n"
        "      - settings\n",
        encoding="utf-8",
    )
    return config


def _write_tracked(repo: Path, md_body: str, yaml_body: str = _YAML_DOC) -> None:
    tracked = repo / "tracked"
    tracked.mkdir(parents=True, exist_ok=True)
    (tracked / "doc.md").write_text(md_body, encoding="utf-8")
    (tracked / "doc.yaml").write_text(yaml_body, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _live_md() -> Path:
    return Path.home() / ".setforge_snap" / "doc.md"


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


def _revert(config: Path, *, extra: list[str] | None = None) -> Result:
    args = ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"]
    if extra:
        args.extend(extra)
    return CliRunner().invoke(app, args)


def _latest_dirname() -> str:
    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    return latest.name


# ---------------------------------------------------------------------------
# back-compat: pre-snapshot transitions
# ---------------------------------------------------------------------------


def test_pre_snapshot_transition_reverts_cleanly_with_stores_untouched(
    repo: Path,
) -> None:
    """A transition without state_snapshots/ (recorded before the bump)
    reverts exactly as before: live restored, stores untouched, exit 0."""
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    shutil.rmtree(latest / "state_snapshots")  # simulate an old record

    base_before_revert = base_store.read_base(_PROFILE, _MD_ID)
    assert base_before_revert is not None

    result = _revert(config)
    assert result.exit_code == 0, result.output
    assert not _live_md().exists()  # live restored (created by install)
    # Stores untouched — the seeded base survives, exactly as a
    # pre-snapshot revert behaved.
    assert base_store.read_base(_PROFILE, _MD_ID) == base_before_revert


# ---------------------------------------------------------------------------
# revert → revert (redo) round-trips the store state
# ---------------------------------------------------------------------------


def test_revert_then_redo_round_trips_store_state(repo: Path) -> None:
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    base_v1 = base_store.read_base(_PROFILE, _MD_ID)
    live_v1 = _live_md().read_bytes()

    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body V2."))
    assert _install(config).exit_code == 0
    base_v2 = base_store.read_base(_PROFILE, _MD_ID)
    live_v2 = _live_md().read_bytes()
    assert base_v2 != base_v1

    assert _revert(config).exit_code == 0
    assert base_store.read_base(_PROFILE, _MD_ID) == base_v1
    assert _live_md().read_bytes() == live_v1

    # Second revert = redo: the reverse transition snapshotted the
    # pre-revert store state, so the redo round-trips back to v2.
    assert _revert(config).exit_code == 0
    assert base_store.read_base(_PROFILE, _MD_ID) == base_v2
    assert _live_md().read_bytes() == live_v2


# ---------------------------------------------------------------------------
# absent vs empty through a real install + revert
# ---------------------------------------------------------------------------


def test_empty_store_entry_restores_as_empty_not_deleted(repo: Path) -> None:
    """A zero-byte store entry pre-install must come back as zero bytes —
    deleting it instead would collapse absent and empty."""
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    # Force the empty-but-present pre-state for the next install. Live and
    # tracked agree, so the 3-way merge resolves trivially and the base
    # advances off the empty state.
    base_store.write_base(_PROFILE, _MD_ID, b"")

    assert _install(config).exit_code == 0
    advanced = base_store.read_base(_PROFILE, _MD_ID)
    assert advanced is not None
    assert advanced != b""

    assert _revert(config).exit_code == 0
    restored = base_store.read_base(_PROFILE, _MD_ID)
    assert restored is not None  # NOT deleted...
    assert restored == b""  # ...rewritten to exactly zero bytes


# ---------------------------------------------------------------------------
# --to-before chain restores newest-first
# ---------------------------------------------------------------------------


def test_to_before_chain_restores_store_state_newest_first(repo: Path) -> None:
    """Reverting to BEFORE install #2 walks #3 then #2, landing live AND
    the stores back at the post-install-#1 state."""
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    base_v1 = base_store.read_base(_PROFILE, _MD_ID)
    live_v1 = _live_md().read_bytes()

    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body V2."))
    assert _install(config).exit_code == 0
    second_dirname = _latest_dirname()

    _write_tracked(repo, _DOC.replace("Shared body original.", "Shared body V3."))
    assert _install(config).exit_code == 0
    assert base_store.read_base(_PROFILE, _MD_ID) != base_v1

    result = _revert(config, extra=[f"--to-before={second_dirname}"])
    assert result.exit_code == 0, result.output
    assert base_store.read_base(_PROFILE, _MD_ID) == base_v1
    assert _live_md().read_bytes() == live_v1
