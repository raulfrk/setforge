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

from setforge import base_store, source, transitions
from setforge.cli import app
from setforge.cli import install as install_mod
from setforge.codex_resources import mcp_target_marker
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


def test_codex_marker_only_install_records_transition_and_revert(repo: Path) -> None:
    codex_home = Path.home() / ".codex"
    codex_home.mkdir()
    live = codex_home / "config.toml"
    desired = (
        b'[mcp_servers.notes]\ncommand = "notes-mcp"\nenabled = true\n'
        b"required = false\n"
    )
    live.write_bytes(desired)
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\nminimum_version: '6.5'\ntracked_files: {}\n"
        "codex:\n  mcp_servers:\n    notes:\n      transport: stdio\n"
        "      command: notes-mcp\nprofiles:\n"
        f"  {_PROFILE}:\n    codex:\n      mcp_servers: [notes]\n"
    )
    digest = sha256(str(live).encode()).hexdigest()[:16]
    config_base_id = file_id(f"codex/config/{digest}")
    marker = mcp_target_marker(live, None)
    marker_id = file_id(marker)
    reconcile_store.write_base(_PROFILE, config_base_id, desired)
    assert reconcile_store.read_base(_PROFILE, marker_id) is None

    installed = _install(config)
    assert installed.exit_code == 0, installed.output
    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    assert (
        transitions.load_meta(latest).command is transitions.TransitionCommand.INSTALL
    )
    assert reconcile_store.read_base(_PROFILE, marker_id) == b""
    assert reconcile_store.read_base(_PROFILE, config_base_id) == desired
    assert live.read_bytes() == desired

    reverted = CliRunner().invoke(
        app,
        ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    assert reconcile_store.read_base(_PROFILE, marker_id) is None
    assert reconcile_store.read_base(_PROFILE, config_base_id) == desired
    assert live.read_bytes() == desired


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


def test_codex_install_converges_empty_desired_prune(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge import codex_plugins as codex_plugins_mod

    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.4'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "codex: {}\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    codex:\n"
        "      reconcile: {policy: prune}\n"
    )
    plugins = {
        "old@official": codex_plugins_mod.InstalledPlugin(
            "old@official", "old", "official"
        )
    }
    monkeypatch.setattr(codex_plugins_mod, "list_installed", lambda: dict(plugins))
    monkeypatch.setattr(codex_plugins_mod, "list_marketplaces", lambda: {})
    monkeypatch.setattr(codex_plugins_mod, "plugin_remove", plugins.pop)

    installed = _install(config)

    assert installed.exit_code == 0, installed.output
    assert plugins == {}
    clean = CliRunner().invoke(
        app,
        ["compare", f"--profile={_PROFILE}", f"--config={config}", "--check"],
    )
    assert clean.exit_code == 0, clean.output


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
    local.parent.mkdir(parents=True, exist_ok=True)
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


def test_codex_mcp_install_compare_and_revert_journey(repo: Path) -> None:
    home = Path.home()
    codex_home = home / ".codex"
    codex_home.mkdir()
    live = codex_home / "config.toml"
    original = b'# host\n[mcp_servers.personal]\ncommand = "mine"\n'
    live.write_bytes(original)
    local = source.LOCAL_CONFIG_PATH
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("codex:\n  environment_vars:\n    api_token: SETFORGE_API_TOKEN\n")
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.5'\n"
        "tracked_files: {}\n"
        "codex:\n"
        "  mcp_servers:\n"
        "    api:\n"
        "      transport: http\n"
        "      url: https://mcp.example.test\n"
        "      bearer_token_env_var: api_token\n"
        "      enabled_tools: [read]\n"
        "      tool_timeout_sec: 20\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    codex:\n"
        "      mcp_servers: [api]\n"
    )

    dry_run = CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--dry-run",
            "--no-git-check",
        ],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert live.read_bytes() == original

    installed = _install(config)
    assert installed.exit_code == 0, installed.output
    rendered = live.read_text()
    assert "[mcp_servers.personal]" in rendered
    assert "[mcp_servers.api]" in rendered
    assert 'bearer_token_env_var = "SETFORGE_API_TOKEN"' in rendered
    assert "api_token" not in rendered

    clean = CliRunner().invoke(
        app,
        ["compare", f"--profile={_PROFILE}", f"--config={config}", "--check"],
    )
    assert clean.exit_code == 0, clean.output
    live.write_text(
        rendered.replace("tool_timeout_sec = 20.0", "tool_timeout_sec = 9.0")
    )
    drifted = CliRunner().invoke(
        app,
        ["compare", f"--profile={_PROFILE}", f"--config={config}", "--check"],
    )
    assert drifted.exit_code == 1
    live.write_text(rendered)

    reverted = CliRunner().invoke(
        app,
        ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    assert live.read_bytes() == original


def test_codex_mcp_sync_preserves_ownership_for_deselect_and_revert(
    repo: Path,
) -> None:
    home = Path.home()
    codex_home = home / ".codex"
    codex_home.mkdir()
    live = codex_home / "config.toml"
    original = b'[mcp_servers.personal]\ncommand = "mine"\n'
    live.write_bytes(original)
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\nminimum_version: '6.5'\ntracked_files: {}\n"
        "codex:\n  mcp_servers:\n    notes:\n      transport: stdio\n"
        "      command: notes-mcp\nprofiles:\n"
        f"  {_PROFILE}:\n    codex:\n      mcp_servers: [notes]\n"
    )

    assert _install(config).exit_code == 0
    selected_live = live.read_bytes()
    marker = mcp_target_marker(live, None)
    assert reconcile_store.read_base(_PROFILE, file_id(marker)) == b""

    synced = CliRunner().invoke(
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
    digest = sha256(str(live).encode()).hexdigest()[:16]
    base = reconcile_store.read_base(_PROFILE, file_id(f"codex/config/{digest}"))
    assert base is not None
    assert b"mcp_servers.notes" in base

    config.write_text(
        config.read_text().replace(
            f"  {_PROFILE}:\n    codex:\n      mcp_servers: [notes]\n",
            f"  {_PROFILE}: {{}}\n",
        )
    )
    deselected = _install(config)
    assert deselected.exit_code == 0, deselected.output
    assert b"mcp_servers.notes" not in live.read_bytes()
    assert b"mcp_servers.personal" in live.read_bytes()
    assert reconcile_store.read_base(_PROFILE, file_id(marker)) is None

    reverted = CliRunner().invoke(
        app,
        ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    assert live.read_bytes() == selected_live
    assert reconcile_store.read_base(_PROFILE, file_id(marker)) == b""


def test_codex_mcp_first_sync_records_marker_for_later_retirement(repo: Path) -> None:
    codex_home = Path.home() / ".codex"
    codex_home.mkdir()
    live = codex_home / "config.toml"
    live.write_bytes(
        b'[mcp_servers.notes]\ncommand = "notes-mcp"\nenabled = true\n'
        b'required = false\n\n[mcp_servers.personal]\ncommand = "mine"\n'
    )
    config = repo / "setforge.yaml"
    selected = (
        "schema_version: '6.5'\nminimum_version: '6.5'\ntracked_files: {}\n"
        "codex:\n  mcp_servers:\n    notes:\n      transport: stdio\n"
        "      command: notes-mcp\nprofiles:\n"
        f"  {_PROFILE}:\n    codex:\n      mcp_servers: [notes]\n"
    )
    config.write_text(selected)

    synced = CliRunner().invoke(
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
    marker = mcp_target_marker(live, None)
    digest = sha256(str(live).encode()).hexdigest()[:16]
    config_base_id = file_id(f"codex/config/{digest}")
    assert reconcile_store.read_base(_PROFILE, file_id(marker)) == b""
    assert reconcile_store.read_base(_PROFILE, config_base_id) is not None
    latest = transitions.load_latest(_PROFILE)
    assert latest is not None
    assert transitions.load_meta(latest).command is transitions.TransitionCommand.SYNC

    reverted = CliRunner().invoke(
        app,
        ["revert", f"--profile={_PROFILE}", f"--config={config}", "--yes"],
    )
    assert reverted.exit_code == 0, reverted.output
    assert reconcile_store.read_base(_PROFILE, file_id(marker)) is None
    assert reconcile_store.read_base(_PROFILE, config_base_id) is None
    assert b"mcp_servers.notes" in live.read_bytes()

    synced_again = CliRunner().invoke(
        app,
        [
            "sync",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--auto=use-live",
            "--yes",
        ],
    )
    assert synced_again.exit_code == 0, synced_again.output
    assert reconcile_store.read_base(_PROFILE, file_id(marker)) == b""

    config.write_text(
        "schema_version: '6.5'\nminimum_version: '6.5'\n"
        f"tracked_files: {{}}\nprofiles:\n  {_PROFILE}: {{}}\n"
    )
    retired = _install(config)
    assert retired.exit_code == 0, retired.output
    assert b"mcp_servers.notes" not in live.read_bytes()
    assert b"mcp_servers.personal" in live.read_bytes()
