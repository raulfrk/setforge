import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import codex_lifecycle, codex_plugins, snapshots
from setforge.cli import app
from setforge.cli._helpers import ProfileContext
from setforge.cli.profile import _profile_show_json_data
from setforge.codex_resources import CodexResourceError
from setforge.compare import CompareReport, CompareStatus
from setforge.config import (
    CodexProfile,
    CodexSpec,
    Config,
    Profile,
    ResolvedProfile,
    resolve_profile,
)
from setforge.errors import PluginToolMissing
from setforge.snapshots import _resolve_dst_paths


def _context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Config, ResolvedProfile]:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    source = tmp_path / "tracked/codex"
    source.mkdir(parents=True)
    (source / "model.toml").write_text('model = "new"\n')
    config = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "config": {"model": {"source": "codex/model.toml"}},
                "marketplaces": {
                    "official": {"source": "path", "path": str(tmp_path / "market")}
                },
                "plugins": {"review": {"marketplace": "official"}},
            }
        ),
        profiles={
            "default": Profile(codex=CodexProfile(config=["model"], plugins=["review"]))
        },
    )
    return config, resolve_profile(config, "default")


def test_projection_reports_native_config_plugin_and_marketplace_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, resolved = _context(tmp_path, monkeypatch)
    monkeypatch.setattr(codex_plugins, "list_installed", lambda: {})
    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: {})
    report = codex_lifecycle.append_projection(
        CompareReport(entries=[], has_unexpected_drift=False),
        config,
        resolved,
        tmp_path,
        profile="default",
    )
    names = [entry.name for entry in report.entries]
    assert names[0].startswith("codex/config/")
    assert names[1:] == ["codex/marketplace/official", "codex/plugin/review@official"]
    assert all(entry.status is CompareStatus.DRIFTED for entry in report.entries)
    assert report.has_unexpected_drift


def test_projection_gives_nonfatal_actionable_missing_plugin_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, resolved = _context(tmp_path, monkeypatch)
    monkeypatch.setattr(
        codex_plugins,
        "list_installed",
        lambda: (_ for _ in ()).throw(PluginToolMissing("codex unavailable")),
    )
    report = codex_lifecycle.append_projection(
        CompareReport(entries=[], has_unexpected_drift=False),
        config,
        resolved,
        tmp_path,
        profile="default",
    )
    diagnostic = next(
        entry for entry in report.entries if entry.name == "codex/plugin-state"
    )
    assert diagnostic.status is CompareStatus.DRIFTED
    assert diagnostic.reason == "Codex plugin state unavailable: codex unavailable"


def test_projection_detects_prune_drift_with_empty_desired_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _resolved = _context(tmp_path, monkeypatch)
    config.profiles["default"].codex = CodexProfile.model_validate(
        {"reconcile": {"policy": "prune"}}
    )
    resolved = resolve_profile(config, "default")
    monkeypatch.setattr(
        codex_plugins,
        "list_installed",
        lambda: {
            "old@official": codex_plugins.InstalledPlugin(
                "old@official", "old", "official"
            )
        },
    )
    monkeypatch.setattr(codex_plugins, "list_marketplaces", lambda: {})
    report = codex_lifecycle.append_projection(
        CompareReport(entries=[], has_unexpected_drift=False),
        config,
        resolved,
        tmp_path,
        profile="default",
    )
    entry = next(
        entry for entry in report.entries if entry.name == "codex/plugin/old@official"
    )
    assert entry.status is CompareStatus.DRIFTED


def test_compare_and_status_share_nonfatal_unavailable_plugin_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '6.4'\n"
        "minimum_version: '6.4'\n"
        "tracked_files: {}\n"
        "codex:\n"
        "  marketplaces:\n"
        "    official: {source: github, repo: owner/repo}\n"
        "  plugins:\n"
        "    review: {marketplace: official}\n"
        "profiles:\n"
        "  default:\n"
        "    codex:\n"
        "      plugins: [review]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        codex_plugins,
        "list_installed",
        lambda: (_ for _ in ()).throw(PluginToolMissing("codex unavailable")),
    )
    compare_result = CliRunner().invoke(
        app,
        [
            "--format=json",
            "compare",
            "--profile=default",
            f"--config={config}",
        ],
    )
    assert compare_result.exit_code == 0, compare_result.output
    compare_data = json.loads(compare_result.stdout)["data"]
    diagnostic = next(
        entry
        for entry in compare_data["entries"]
        if entry["name"] == "codex/plugin-state"
    )
    assert diagnostic["status"] == "drifted"
    assert "Codex plugin state unavailable" in diagnostic["reason"]

    status_result = CliRunner().invoke(
        app,
        [
            "--source",
            str(tmp_path),
            "--format=json",
            "status",
            "--profile=default",
            f"--config={config}",
        ],
    )
    assert status_result.exit_code == 0, status_result.output
    assert json.loads(status_result.stdout)["data"]["drift"]["drifted"] == 1

    checked = CliRunner().invoke(
        app,
        [
            "compare",
            "--check",
            "--profile=default",
            f"--config={config}",
        ],
    )
    assert checked.exit_code == 1


def test_profile_json_and_snapshot_paths_include_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, resolved = _context(tmp_path, monkeypatch)
    payload = _profile_show_json_data(
        ProfileContext(
            cfg=config, resolved=resolved, repo_root=tmp_path, profile="default"
        )
    )
    assert payload["codex"] == {
        "config": ["model"],
        "instructions": [],
        "skills": [],
        "plugins": ["review"],
        "mcp_servers": [],
    }
    paths = _resolve_dst_paths(config, resolved, tmp_path, profile="default")
    assert tmp_path / "codex-home/config.toml" in paths


def test_snapshot_round_trip_restores_complete_native_codex_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, resolved = _context(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    destination = tmp_path / "codex-home/config.toml"
    original = b'# personal\napproval_policy = "never"\nmodel = "old"\n'
    destination.write_bytes(original)
    meta = snapshots.create_snapshot(
        config, resolved, tmp_path, "default", "codex-native", keep=10
    )
    destination.write_bytes(
        b'# changed\napproval_policy = "on-request"\nmodel = "new"\n'
    )

    snapshots.restore_snapshot(
        meta.snapshot_id,
        pre_snapshot=False,
        pre_snapshot_ctx=snapshots.PreSnapshotCtx(
            cfg=config,
            resolved=resolved,
            repo_root=tmp_path,
            profile="default",
        ),
    )

    assert destination.read_bytes() == original


def test_snapshot_refuses_untrusted_project_codex_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "tracked/codex"
    source.mkdir(parents=True)
    (source / "project.toml").write_text('model = "new"\n')
    config = Config(
        schema_version="6.4",
        minimum_version="6.4",
        tracked_files={},
        codex=CodexSpec.model_validate(
            {
                "config": {
                    "project": {
                        "source": "codex/project.toml",
                        "scope": "project",
                        "project": "app",
                    }
                }
            }
        ),
        profiles={"default": Profile(codex=CodexProfile(config=["project"]))},
    )
    config._codex_project_paths = {"app": project}
    resolved = resolve_profile(config, "default")

    with pytest.raises(CodexResourceError, match="not trusted"):
        snapshots.create_snapshot(
            config, resolved, tmp_path, "default", "untrusted", keep=10
        )
