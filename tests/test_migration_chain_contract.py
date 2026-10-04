"""Current-schema chains preserve intent and retain one-step byte recovery."""

from itertools import pairwise
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import reconcile, transitions
from setforge.cli import app
from setforge.config import load_config, resolve_profile
from setforge.migrations import detect_current_schema
from setforge.migrations.registry import MIGRATIONS, find_migration_path
from setforge.reconcile.types import file_id

_VERSIONS = (
    "1.0",
    "1.1",
    "1.2",
    "2.0",
    "2.1",
    "3.0",
    "4.0",
    "5.0",
    "6.0",
    "6.1",
    "6.2",
    "6.3",
    "6.4",
    "6.5",
)


def test_registry_has_each_forward_and_reverse_edge() -> None:
    edges = list(pairwise(_VERSIONS))
    assert [(step.from_version, step.to_version) for step in MIGRATIONS] == edges
    for before, after in edges:
        up = find_migration_path(from_v=before, to_v=after)
        down = find_migration_path(from_v=after, to_v=before)
        assert [(step.from_version, step.to_version) for step in up] == [
            (before, after)
        ]
        assert [(step.from_version, step.to_version) for step in down] == [
            (after, before)
        ]


@pytest.mark.parametrize("origin", ["2.1", "3.0", "4.0", "6.5"])
def test_lossy_reverse_chain_refuses_without_partial_restamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "setforge.yaml"
    original = (
        f"schema_version: '{origin}'\ntracked_files: {{}}\nprofiles: {{base: {{}}}}\n"
    ).encode()
    config.write_bytes(original)

    result = CliRunner().invoke(
        app,
        ["migrate", "--config", str(config), "--to", "2.0", "--apply", "--yes"],
    )

    assert result.exit_code != 0
    assert "setforge revert --profile=migrate" in " ".join(result.output.split())
    assert config.read_bytes() == original
    assert transitions.list_transitions(["migrate"]) == []


@pytest.mark.parametrize("version", _VERSIONS[:-1])
def test_current_schema_chain_reverts_to_each_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "setforge.yaml"
    original = (
        f"# origin {version}\nschema_version: '{version}'\n"
        "minimum_version: '6.5'\ntracked_files: {}\nprofiles: {base: {}}\n"
    ).encode()
    config.write_bytes(original)
    runner = CliRunner()

    applied = runner.invoke(
        app, ["migrate", "--config", str(config), "--apply", "--yes"]
    )

    assert applied.exit_code == 0, applied.output + str(applied.exception)
    assert detect_current_schema(config) == "6.5"
    assert load_config(config).profiles.keys() == {"base"}
    assert len(transitions.list_transitions(["migrate"])) == 1

    reverted = runner.invoke(
        app, ["revert", "--profile=migrate", "--config", str(config), "--yes"]
    )

    assert reverted.exit_code == 0, reverted.output + str(reverted.exception)
    assert config.read_bytes() == original


@pytest.mark.parametrize("version", ["1.0", "1.2", "2.0", "2.1", "3.0", "4.0", "5.0"])
def test_legacy_profile_fields_survive_chain_to_current_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "setforge.yaml"
    original = (
        f"schema_version: '{version}'\nminimum_version: '6.5'\n"
        "tracked_files: {}\n"
        "marketplaces: {catalog: {source: github, repo: example/catalog}}\n"
        "claude_plugins: {helper: {marketplace: catalog}}\n"
        "profiles:\n  base:\n    cargo_binaries: [rg]\n"
        "    claude_plugins: [helper]\n    plugins_reconcile: prune\n"
        "    extensions:\n      include: [ms-python.python]\n"
        "      exclude: [GitHub.copilot]\n      reconcile: prune\n"
        "  child:\n    extends: base\n    cargo_binaries: [fd]\n"
    ).encode()
    config.write_bytes(original)
    runner = CliRunner()

    applied = runner.invoke(
        app, ["migrate", "--config", str(config), "--apply", "--yes"]
    )

    assert applied.exit_code == 0, applied.output + str(applied.exception)
    loaded = load_config(config)
    assert loaded.schema_version == "6.5"
    resolved = resolve_profile(loaded, "child")
    assert resolved.packages == ["rg", "helper", "ms-python.python", "fd"]
    assert resolved.reconcile.plugins.policy == "prune"
    assert resolved.reconcile.extensions.policy == "prune"
    assert resolved.reconcile.extensions.exclude == ["GitHub.copilot"]
    assert len(transitions.list_transitions(["migrate"])) == 1

    reverted = runner.invoke(
        app, ["revert", "--profile=migrate", "--config", str(config), "--yes"]
    )

    assert reverted.exit_code == 0, reverted.output + str(reverted.exception)
    assert config.read_bytes() == original


def test_legacy_package_and_host_section_chain_restores_all_original_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "setforge.yaml"
    config.write_text(
        "schema_version: '2.1'\nminimum_version: '6.5'\n"
        "tracked_files:\n  notes: {src: notes.md, dst: ~/notes.md}\n"
        "profiles:\n  base:\n    tracked_files: [notes]\n    cargo_binaries: [rg]\n"
    )
    source = tmp_path / "tracked" / "notes.md"
    source.parent.mkdir()
    source.write_text("## Notes\nshared\n")
    live = Path.home() / "notes.md"
    live.write_bytes(source.read_bytes())
    local = Path.home() / ".config/setforge/local.yaml"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(
        "tracked_files:\n  notes:\n    host_local_sections:\n      host:\n"
        "        anchor: {kind: after-heading, value: Notes}\n"
        '        body: "## Host\\nhost-only\\n"\n'
    )
    original = {path: path.read_bytes() for path in (config, source, live, local)}
    runner = CliRunner()

    applied = runner.invoke(
        app, ["migrate", "--config", str(config), "--apply", "--yes"]
    )

    assert applied.exit_code == 0, applied.output + str(applied.exception)
    loaded = load_config(config)
    assert loaded.schema_version == "6.5"
    assert resolve_profile(loaded, "base").packages == ["rg"]
    assert reconcile.read_base("base", file_id("notes")) == b"## Notes\nshared\n"
    assert reconcile.read_local("base", file_id("notes")) == (
        b"## Notes\n## Host\nhost-only\nshared\n"
    )
    assert len(transitions.list_transitions(["migrate"])) == 1

    reverted = runner.invoke(
        app, ["revert", "--profile=migrate", "--config", str(config), "--yes"]
    )

    assert reverted.exit_code == 0, reverted.output + str(reverted.exception)
    assert {path: path.read_bytes() for path in original} == original
    assert reconcile.read_base("base", file_id("notes")) is None
    assert reconcile.read_local("base", file_id("notes")) is None
