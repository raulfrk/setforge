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

from hashlib import sha256
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import base_store, transitions
from setforge.cli import app
from setforge.cli import install as install_mod
from setforge.reconcile import store as reconcile_store
from setforge.reconcile.types import HunkClass, UnitRef, file_id
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


def test_codex_base_only_install_records_transition_and_revert(repo: Path) -> None:
    home = Path.home()
    codex_home = home / ".codex"
    codex_home.mkdir()
    desired = b'model = "gpt-5"\n'
    (codex_home / "config.toml").write_bytes(desired)
    tracked = repo / "tracked/codex"
    tracked.mkdir(parents=True)
    (tracked / "model.toml").write_bytes(desired)
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.4'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "codex:\n"
        "  config:\n"
        "    model:\n"
        "      source: codex/model.toml\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    codex:\n"
        "      config: [model]\n"
    )
    destination = codex_home / "config.toml"
    digest = sha256(str(destination).encode()).hexdigest()[:16]
    resource_id = f"codex/config/{digest}"

    installed = _install(config)
    assert installed.exit_code == 0, installed.output
    assert reconcile_store.read_base(_PROFILE, file_id(resource_id)) == desired
    assert transitions.load_latest(_PROFILE) is not None

    reverted = CliRunner().invoke(
        app,
        ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    assert reconcile_store.read_base(_PROFILE, file_id(resource_id)) is None
    assert destination.read_bytes() == desired


def test_codex_install_compare_sync_and_revert_journey(repo: Path) -> None:
    codex_home = Path.home() / ".codex"
    codex_home.mkdir()
    live = codex_home / "config.toml"
    live.write_text('model = "old"\napproval_policy = "never"\n')
    tracked = repo / "tracked/codex"
    tracked.mkdir(parents=True)
    fragment = tracked / "model.toml"
    fragment.write_text('model = "old"\n')
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.4'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "codex:\n"
        "  config:\n"
        "    model:\n"
        "      source: codex/model.toml\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    codex:\n"
        "      config: [model]\n"
    )
    assert _install(config).exit_code == 0
    runner = CliRunner()
    clean = runner.invoke(
        app,
        ["compare", f"--profile={_PROFILE}", f"--config={config}", "--check"],
    )
    assert clean.exit_code == 0, clean.output

    live.write_text('model = "local"\napproval_policy = "never"\n')
    drifted = runner.invoke(
        app,
        [
            "compare",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--check",
            "--strict",
        ],
    )
    assert drifted.exit_code == 1
    assert "codex/config/" in drifted.output

    synced = runner.invoke(
        app,
        [
            "sync",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--auto=use-live",
            "--yes",
        ],
    )
    assert synced.exit_code == 0, synced.output
    assert fragment.read_text() == 'model = "local"\n'
    assert (
        runner.invoke(
            app,
            ["compare", f"--profile={_PROFILE}", f"--config={config}", "--check"],
        ).exit_code
        == 0
    )

    reverted = runner.invoke(
        app,
        ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    assert fragment.read_text() == 'model = "old"\n'


def test_install_refuses_project_resource_after_trust_revocation(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = repo.parent / "project"
    project.mkdir()
    codex_home = Path.home() / ".codex"
    codex_home.mkdir()
    trust = codex_home / "config.toml"
    trust.write_text(f'[projects."{project}"]\ntrust_level = "trusted"\n')
    local = Path.home() / ".config/setforge/local.yaml"
    local.parent.mkdir(parents=True)
    local.write_text(f"codex:\n  project_paths:\n    app: {project}\n")
    monkeypatch.setattr(install_mod.source_mod, "LOCAL_CONFIG_PATH", local)
    tracked = repo / "tracked/codex"
    tracked.mkdir(parents=True)
    (tracked / "AGENTS.md").write_text("project instructions\n")
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.4'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "codex:\n"
        "  instructions:\n"
        "    project:\n"
        "      source: codex/AGENTS.md\n"
        "      scope: project\n"
        "      project: app\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    codex:\n"
        "      instructions: [project]\n"
    )
    original_validate = install_mod._validate_external_plan
    planned_projects: list[tuple[Path, ...]] = []

    def revoke_after_plan(plan: install_mod.InstallPlan) -> None:
        planned_projects.append(plan.codex_trusted_projects)
        original_validate(plan)
        trust.write_text(f'[projects."{project}"]\ntrust_level = "untrusted"\n')

    monkeypatch.setattr(install_mod, "_validate_external_plan", revoke_after_plan)

    result = _install(config)

    assert planned_projects == [(project,)]
    assert result.exit_code != 0
    assert result.exception is not None
    assert "trust changed after planning" in str(result.exception)
    assert not (project / "AGENTS.md").exists()


def test_retired_reconcile_data_is_pruned_and_revert_restores_it(repo: Path) -> None:
    _write_tracked(repo, _DOC)
    config = _write_config(repo)
    assert _install(config).exit_code == 0

    fid = file_id(_FILE_ID)
    base = reconcile_store.read_base(_PROFILE, fid)
    local = reconcile_store.read_local(_PROFILE, fid)
    assert base is not None
    assert isinstance(local, bytes)
    unit_id = "sha256:retired-draft"
    draft = b"Shareable retired draft\n"
    reconcile_store.record(
        _PROFILE,
        fid,
        base=base,
        local=local,
        staged=True,
        hunks=[
            {
                "kind": "line",
                "cls": HunkClass.SHARED_DRAFTED.value,
                "label": "## Notes",
                "live_hash": "sha256:live",
                "unit_id": unit_id,
                "draft_hash": reconcile_store.content_sha(draft),
            }
        ],
        drafts={UnitRef.line(unit_id): draft},
    )
    paths = {
        SnapshotStore.BASE: base_store.base_path(_PROFILE, _FILE_ID),
        SnapshotStore.LOCAL_CONTENT: reconcile_store.local_content_path(
            _PROFILE, _FILE_ID
        ),
        SnapshotStore.LOCAL_ABSENT: reconcile_store.local_absent_path(
            _PROFILE, _FILE_ID
        ),
        SnapshotStore.DRAFTS: reconcile_store.drafts_manifest_path(_PROFILE, _FILE_ID),
        SnapshotStore.INDEX: reconcile_store.index_manifest_path(_PROFILE),
    }
    before = {
        kind: path.read_bytes() if path.exists() else None
        for kind, path in paths.items()
    }

    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "    tracked_files:\n      - doc\n", "    tracked_files: []\n"
        ),
        encoding="utf-8",
    )
    retired = _install(config)
    assert retired.exit_code == 0, retired.output

    assert reconcile_store.stored_file_ids(_PROFILE) == set()
    assert reconcile_store.read_index(_PROFILE).files == {}
    snapshots = _latest_snapshots()
    assert {(entry.store, entry.key) for entry in snapshots} == {
        (SnapshotStore.BASE, _FILE_ID),
        (SnapshotStore.LOCAL_CONTENT, _FILE_ID),
        (SnapshotStore.LOCAL_ABSENT, _FILE_ID),
        (SnapshotStore.DRAFTS, _FILE_ID),
        (SnapshotStore.INDEX, _PROFILE),
    }
    assert {entry.store: entry.payload for entry in snapshots} == before

    reverted = CliRunner().invoke(
        app,
        [
            "revert",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--yes",
        ],
    )
    assert reverted.exit_code == 0, reverted.output
    assert {
        kind: path.read_bytes() if path.exists() else None
        for kind, path in paths.items()
    } == before
    reconcile_store.verify(_PROFILE)
