"""Active native resources must not become tracked-file cleanup candidates."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge import compare as compare_mod
from setforge import orphan_scan, transitions
from setforge.cli import app
from setforge.compare import _touched_paths_from_meta
from setforge.config import Config, Profile, TrackedFile, load_config, resolve_profile
from setforge.file_ownership import file_resource_id
from setforge.locking import mutation_locks
from setforge.ownership import OwnershipStore
from tests.test_orphans import _strip_ansi_and_newlines, _write_meta_record


@pytest.fixture
def native_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    source = repo / "tracked"
    (source / "skill/references").mkdir(parents=True)
    (source / "skill/empty").mkdir()
    (source / "skill/SKILL.md").write_text(
        "---\nname: smoke\ndescription: Regression fixture\n---\nSkill instructions.\n"
    )
    (source / "skill/references/guide.md").write_text("Reference content.\n")
    (source / "instructions.md").write_text("Shared instructions.\n")
    (source / "model.toml").write_text('model = "managed"\n')
    (source / "retired.txt").write_text("Previously deployed content.\n")
    live = Path.home() / ".codex"
    live.mkdir(parents=True)
    monkeypatch.setattr(
        compare_mod,
        "GENERIC_DST_ROOTS",
        compare_mod.GENERIC_DST_ROOTS | {Path.home()},
    )
    (live / "config.toml").write_text('personal = "preserve"\n')
    monkeypatch.setenv("CODEX_HOME", str(live))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = repo / "setforge.yaml"
    document = {
        "schema_version": "6.5",
        "minimum_version": "6.4",
        "tracked_files": {
            "retired": {"src": "retired.txt", "dst": str(live / "retired.txt")}
        },
        "codex": {
            "config": {"model": {"source": "model.toml"}},
            "instructions": {"guide": {"source": "instructions.md"}},
            "skills": {"smoke": {"source": "skill"}},
        },
        "profiles": {
            "p": {
                "tracked_files": ["retired"],
                "codex": {
                    "config": ["model"],
                    "instructions": ["guide"],
                    "skills": ["smoke"],
                },
            }
        },
    }
    YAML().dump(document, config)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    installed = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--yes",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
        ],
    )
    assert installed.exit_code == 0, installed.output
    assert (live / "config.toml").read_text() == (
        'personal = "preserve"\nmodel = "managed"\n'
    )
    skill = live / "skills/smoke"
    assert (skill / "empty").is_dir()
    assert (skill / "SKILL.md").read_bytes() == (source / "skill/SKILL.md").read_bytes()
    # History can record both a deployed tree and the paths beneath it.
    _write_meta_record(
        transitions.transitions_root(),
        "recorded-tree-paths",
        [
            str(skill),
            str(skill / "references"),
            str(skill / "references/guide.md"),
            str(skill / "empty"),
        ],
    )
    assert live / "config.toml" in _touched_paths_from_meta(
        transitions.transitions_root()
    )
    return config


def _active_paths() -> list[Path]:
    live = Path.home() / ".codex"
    return [
        live / "config.toml",
        live / "AGENTS.md",
        *sorted((live / "skills").rglob("*")),
    ]


def _path_state(paths: list[Path]) -> dict[Path, tuple[int, int, bytes | None]]:
    return {
        path: (
            path.stat().st_ino,
            path.stat().st_mode,
            None if path.is_dir() else path.read_bytes(),
        )
        for path in paths
    }


def _state_bytes(state: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in state.rglob("*")
        if path.is_file() and "locks" not in path.relative_to(state).parts
    }


def test_declared_empty_tree_root_is_not_an_orphan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tracked/empty").mkdir(parents=True)
    (repo / "tracked/anchor").write_text("anchor\n")
    live = tmp_path / "live"
    root = live / "empty"
    root.mkdir(parents=True)
    (live / "anchor").write_text("anchor\n")
    config = Config(
        tracked_files={
            "empty": TrackedFile(src=Path("empty"), dst=str(root)),
            "anchor": TrackedFile(src=Path("anchor"), dst=str(live / "anchor")),
        },
        profiles={"p": Profile(tracked_files=["empty", "anchor"])},
    )
    history = tmp_path / "history"
    _write_meta_record(history, "install-empty", [str(root)])

    report = compare_mod.detect_orphans(
        resolve_profile(config, "p"), config, history, repo
    )

    assert report.orphans == []


@pytest.mark.parametrize("json_output", [False, True])
def test_compare_keeps_active_native_config_and_skill_directories(
    native_profile: Path, json_output: bool
) -> None:
    args = ["compare", "--profile=p", f"--config={native_profile}"]
    if json_output:
        args.insert(0, "--format=json")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    if json_output:
        report = json.loads(result.stdout)["data"]
        assert report["orphans"] == []
        assert any(
            entry["name"].startswith("codex/config/") for entry in report["entries"]
        )
    else:
        assert "Orphans (" not in result.output
        assert "cleanup-orphans" not in result.output


@pytest.mark.parametrize("apply", [False, True])
def test_cleanup_preserves_active_native_resources_and_private_state(
    native_profile: Path, tmp_path: Path, apply: bool
) -> None:
    paths = _active_paths()
    before = _path_state(paths)
    state_before = _state_bytes(tmp_path / "state")
    args = ["cleanup-orphans", "--profile=p", f"--config={native_profile}"]
    if apply:
        args += ["--apply", "--yes"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "no orphans" in result.output
    assert _path_state(paths) == before
    assert _state_bytes(tmp_path / "state") == state_before


def test_cleanup_still_removes_a_retired_tracked_neighbor(
    native_profile: Path, tmp_path: Path
) -> None:
    retired = Path.home() / ".codex/retired.txt"
    yaml = YAML()
    document = yaml.load(native_profile.read_text())
    document["tracked_files"] = {}
    document["profiles"]["p"]["tracked_files"] = []
    yaml.dump(document, native_profile)
    store = OwnershipStore()
    resource = file_resource_id(retired)
    claim = store.read(resource)
    assert claim is not None
    with mutation_locks(resources=True):
        store.release_locked(
            resource,
            expected_owner=claim.owner_id,
            expected_generation=claim.generation,
        )
    paths = _active_paths()
    before = _path_state(paths)
    claims_before = store.list_claims()
    runner = CliRunner()
    compared = runner.invoke(
        app, ["--format=json", "compare", "--profile=p", f"--config={native_profile}"]
    )
    assert compared.exit_code == 0, compared.output
    assert json.loads(compared.stdout)["data"]["orphans"] == [str(retired)]
    args = ["cleanup-orphans", "--profile=p", f"--config={native_profile}"]
    preview = runner.invoke(app, args)
    assert preview.exit_code == 0, preview.output
    assert str(retired) in _strip_ansi_and_newlines(preview.output)
    applied = runner.invoke(app, [*args, "--apply", "--yes"])
    assert applied.exit_code == 0, applied.output
    assert not retired.exists()
    assert _path_state(paths) == before
    assert store.list_claims() == claims_before


@pytest.mark.parametrize("profile", ["files", "child"])
@pytest.mark.parametrize("retired_native_selection", [False, True])
def test_cleanup_protects_native_config_from_another_profile(
    native_profile: Path, profile: str, retired_native_selection: bool
) -> None:
    yaml = YAML()
    document = yaml.load(native_profile.read_text())
    document["profiles"]["files"] = {
        "tracked_files": ["retired"],
        "codex": {"instructions": ["guide"], "skills": ["smoke"]},
    }
    document["profiles"]["child"] = {"extends": "files"}
    if retired_native_selection:
        document["profiles"]["p"]["codex"]["config"] = []
    yaml.dump(document, native_profile)
    paths = _active_paths()
    before = _path_state(paths)
    runner = CliRunner()
    compared = runner.invoke(
        app,
        [
            "--format=json",
            "compare",
            f"--profile={profile}",
            f"--config={native_profile}",
        ],
    )
    assert compared.exit_code == 0, compared.output
    assert json.loads(compared.stdout)["data"]["orphans"] == []

    stray = Path.home() / ".codex/old.txt"
    stray.write_text("retired deployment\n")
    _write_meta_record(transitions.transitions_root(), "old-deployment", [str(stray)])
    args = ["cleanup-orphans", f"--profile={profile}", f"--config={native_profile}"]
    preview = runner.invoke(app, args)
    assert preview.exit_code == 0, preview.output
    assert str(stray) in _strip_ansi_and_newlines(preview.output)
    assert "config.toml" not in preview.output
    applied = runner.invoke(app, [*args, "--apply", "--yes"])
    assert applied.exit_code == 0, applied.output
    assert not stray.exists()
    assert _path_state(paths) == before


@pytest.mark.parametrize("profile", ["p", "files", "child"])
@pytest.mark.parametrize(
    "installed", [False, True], ids=["no-history", "pruned-history"]
)
def test_scan_protects_native_config_without_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, installed: bool
) -> None:
    repo = tmp_path / "repo"
    source = repo / "tracked"
    source.mkdir(parents=True)
    (source / "model.toml").write_text('model = "managed"\n')
    (source / "instructions.md").write_text("instructions\n")
    live = Path.home() / ".codex"
    live.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(live))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = repo / "setforge.yaml"
    YAML().dump(
        {
            "schema_version": "6.5",
            "minimum_version": "6.4",
            "tracked_files": {},
            "codex": {
                "config": {"model": {"source": "model.toml"}},
                "instructions": {"guide": {"source": "instructions.md"}},
            },
            "profiles": {
                "p": {"codex": {"config": ["model"], "instructions": ["guide"]}},
                "files": {"codex": {"instructions": ["guide"]}},
                "child": {"extends": "p"},
            },
        },
        config,
    )
    (live / "config.toml").write_text('personal = "preserve"\n')
    (live / "AGENTS.md").write_text("instructions\n")
    if installed:
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        result = CliRunner().invoke(
            app,
            [
                "install",
                "--profile=p",
                f"--config={config}",
                "--yes",
                "--no-fetch",
                "--no-git-check",
                "--no-secrets-scan",
            ],
        )
        assert result.exit_code == 0, result.output
        shutil.rmtree(transitions.transitions_root())
    stray = live / "unrecorded.txt"
    stray.write_text("unrecorded neighbor\n")
    before = _path_state([live / "config.toml", live / "AGENTS.md", stray])
    scan = orphan_scan.scan_unrecorded_managed_tree(
        load_config(config),
        repo,
        config_path=config,
        transitions_dir=transitions.transitions_root(),
    )
    assert [entry.path for entry in scan.entries] == [stray]
    preview = CliRunner().invoke(
        app, ["cleanup-orphans", "--scan", f"--profile={profile}", f"--config={config}"]
    )
    assert preview.exit_code == 0, preview.output
    assert str(stray) in _strip_ansi_and_newlines(preview.output)
    assert "config.toml" not in preview.output
    assert _path_state(list(before)) == before
