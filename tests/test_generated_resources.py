from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer
from click.testing import Result
from typer.testing import CliRunner

from setforge.capture import capture_profile, preview_capture_profile
from setforge.cli import app
from setforge.cli.stage import _refuse_generated_stage_target
from setforge.compare import CompareStatus, compare_profile
from setforge.config import (
    Config,
    GeneratedContent,
    HostInputKind,
    Profile,
    TrackedFile,
    load_config,
    resolve_profile,
)
from setforge.errors import ConfigError, InvariantViolation
from setforge.file_ownership import file_resource_id, observe_file
from setforge.generated import resolve_generated
from setforge.ownership import OwnershipStore, ProvenanceFactKind, read_owner_id


def _generated() -> GeneratedContent:
    return GeneratedContent(
        inputs={
            "home": HostInputKind.HOME,
            "code": HostInputKind.VSCODE_USER_DIR,
        }
    )


def test_generated_resolution_is_deterministic_and_namespaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    first = resolve_generated("{{ host.home }}\n{{ host.code }}\n", _generated())
    second = resolve_generated("{{ host.home }}\n{{ host.code }}\n", _generated())

    assert first == second
    assert first.rendered.startswith(str((tmp_path / "home").resolve()))
    assert "{{" not in first.rendered
    assert tuple(name for name, _kind, _value in first.inputs) == ("code", "home")


def test_generated_resolution_refuses_ambient_or_undeclared_values() -> None:
    with pytest.raises(ConfigError, match="generated tracked-file"):
        resolve_generated("{{ home }}", _generated())
    with pytest.raises(ConfigError, match="generated tracked-file"):
        resolve_generated("{{ host.missing }}", _generated())


@pytest.mark.parametrize(
    "source",
    [
        "{{ cycler.__init__.__globals__.os.environ }}",
        "{{ lipsum.__globals__['os'].name }}",
        "{{ host.home.__class__.__mro__ }}",
        "{{ host.home.upper() }}",
    ],
)
def test_generated_resolution_refuses_globals_attributes_and_calls(source: str) -> None:
    with pytest.raises(ConfigError, match="generated tracked-file"):
        resolve_generated(source, _generated())


@pytest.mark.parametrize("name", ["items", "keys", "get", "values"])
def test_generated_resolution_input_names_do_not_collide_with_mapping_methods(
    name: str,
) -> None:
    spec = GeneratedContent(inputs={name: HostInputKind.HOME})

    resolution = resolve_generated(f"{{{{ host.{name} }}}}", spec)

    assert resolution.rendered == str(Path.home().resolve())


@pytest.mark.parametrize(
    "source",
    [
        "{{ [1, 2, 3, 4, 5] | random }}",
        "{{ host.home | upper }}",
        "{{ host.home is string }}",
    ],
)
def test_generated_resolution_refuses_filters_and_tests(source: str) -> None:
    with pytest.raises(ConfigError, match="filters or tests"):
        resolve_generated(source, _generated())


@pytest.mark.parametrize(
    "source",
    [
        "{{ host.home.upper }}",
        "{{ host.home.format }}",
        "{{ host.home.encode }}",
        "{{ host['home'] }}",
    ],
)
def test_generated_resolution_refuses_deeper_attributes_and_indexing(
    source: str,
) -> None:
    with pytest.raises(ConfigError, match=r"only read declared host\.<name>"):
        resolve_generated(source, _generated())


def test_generated_input_names_are_closed_public_identifiers() -> None:
    with pytest.raises(ValueError, match="public identifier"):
        GeneratedContent(inputs={"bad-name": HostInputKind.HOME})
    with pytest.raises(ValueError, match="must not be empty"):
        GeneratedContent(inputs={})


@pytest.mark.parametrize(
    ("schema", "minimum"),
    [("6.0", "6.1"), ("6.1", None), ("6.1", "6.0")],
)
def test_generated_config_requires_six_one_reader_floor(
    tmp_path: Path, schema: str, minimum: str | None
) -> None:
    config = tmp_path / "setforge.yaml"
    floor = "" if minimum is None else f"minimum_version: '{minimum}'\n"
    config.write_text(
        f"schema_version: '{schema}'\n"
        f"{floor}"
        "tracked_files:\n"
        "  x:\n"
        "    src: x.j2\n"
        "    dst: ~/x\n"
        "    generated:\n"
        "      inputs: {home: home}\n"
        "profiles: {}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"require.*6.1"):
        load_config(config)


def _config(repo: Path, live: Path) -> Config:
    return Config(
        tracked_files={
            "generated": TrackedFile(
                src=Path("generated.txt.j2"),
                dst=str(live),
                generated=_generated(),
            )
        },
        profiles={"p": Profile(tracked_files=["generated"])},
    )


def test_compare_uses_rendered_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    tracked = repo / "tracked" / "generated.txt.j2"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("home={{ host.home }}\n", encoding="utf-8")
    live = tmp_path / "live" / "generated.txt"
    live.parent.mkdir()
    live.write_text(f"home={(tmp_path / 'home').resolve()}\n", encoding="utf-8")

    report = compare_profile(_config(repo, live), "p", repo)

    assert report.entries[0].status is CompareStatus.UNCHANGED


def test_stage_refuses_generated_target(tmp_path: Path) -> None:
    config = _config(tmp_path, tmp_path / "live")
    resolved = resolve_profile(config, "p")

    with pytest.raises(typer.BadParameter, match="one-way output"):
        _refuse_generated_stage_target(config, resolved, "generated")


def test_capture_preview_and_apply_refuse_generated_output_before_source_write(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    tracked = repo / "tracked" / "generated.txt.j2"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("portable={{ host.home }}\n", encoding="utf-8")
    live = tmp_path / "live" / "generated.txt"
    live.parent.mkdir()
    live.write_text("host-specific\n", encoding="utf-8")
    config = _config(repo, live)
    resolved = resolve_profile(config, "p")

    with pytest.raises(InvariantViolation, match="one-way output"):
        preview_capture_profile(config, "p", repo, resolved=resolved)
    with pytest.raises(InvariantViolation, match="one-way output"):
        capture_profile(
            config,
            "p",
            repo,
            setforge_yaml_path=repo / "setforge.yaml",
            resolved=resolved,
        )
    assert tracked.read_text(encoding="utf-8") == "portable={{ host.home }}\n"


def test_capture_profile_refuses_generated_file_before_earlier_regular_write(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    tracked = repo / "tracked"
    tracked.mkdir(parents=True)
    ordinary_src = tracked / "ordinary"
    ordinary_src.write_text("tracked\n", encoding="utf-8")
    generated_src = tracked / "generated.j2"
    generated_src.write_text("{{ host.home }}\n", encoding="utf-8")
    ordinary_live = tmp_path / "live" / "ordinary"
    generated_live = tmp_path / "live" / "generated"
    ordinary_live.parent.mkdir()
    ordinary_live.write_text("would capture\n", encoding="utf-8")
    generated_live.write_text("host output\n", encoding="utf-8")
    config = Config(
        tracked_files={
            "ordinary": TrackedFile(src=Path("ordinary"), dst=str(ordinary_live)),
            "generated": TrackedFile(
                src=Path("generated.j2"),
                dst=str(generated_live),
                generated=GeneratedContent(inputs={"home": HostInputKind.HOME}),
            ),
        },
        profiles={"p": Profile(tracked_files=["ordinary", "generated"])},
    )

    with pytest.raises(InvariantViolation, match="one-way output"):
        capture_profile(
            config,
            "p",
            repo,
            setforge_yaml_path=repo / "setforge.yaml",
            resolved=resolve_profile(config, "p"),
        )

    assert ordinary_src.read_text(encoding="utf-8") == "tracked\n"
    assert generated_src.read_text(encoding="utf-8") == "{{ host.home }}\n"


def test_install_renders_and_records_generator_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "generated.txt.j2").write_text(
        "home={{ host.home }}\n", encoding="utf-8"
    )
    config = repo / "setforge.yaml"
    live = home / ".config" / "generated.txt"
    config.write_text(
        "schema_version: '6.1'\n"
        "minimum_version: '6.1'\n"
        "tracked_files:\n"
        "  generated:\n"
        "    src: generated.txt.j2\n"
        "    dst: ~/.config/generated.txt\n"
        "    generated:\n"
        "      inputs:\n"
        "        home: home\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [generated]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)

    result = CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert live.read_text(encoding="utf-8") == f"home={home.resolve()}\n"
    claim = OwnershipStore().read(file_resource_id(live))
    assert claim is not None
    assert {fact.kind for fact in claim.provenance} >= {
        ProvenanceFactKind.GENERATOR,
        ProvenanceFactKind.RESOLVER,
        ProvenanceFactKind.INTEGRITY,
    }


def _write_generated_repo(repo: Path, live: Path) -> Path:
    (repo / "tracked").mkdir(parents=True)
    (repo / "tracked" / "generated.txt.j2").write_text(
        "home={{ host.home }}\n", encoding="utf-8"
    )
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.1'\n"
        "minimum_version: '6.1'\n"
        "tracked_files:\n"
        "  generated:\n"
        "    src: generated.txt.j2\n"
        f"    dst: {live}\n"
        "    generated:\n"
        "      inputs: {home: home}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [generated]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    return config


def _install(config: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--no-fetch",
            "--no-git-check",
            "--no-secrets-scan",
            "--yes",
            *extra,
        ],
    )


def test_generated_adoption_is_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "live" / "generated.txt"
    live.parent.mkdir()
    live.write_text("external\n", encoding="utf-8")
    config = _write_generated_repo(tmp_path / "repo", live)

    result = _install(config)

    assert result.exit_code == 0, result.output
    assert live.read_text(encoding="utf-8") == "external\n"
    claim = OwnershipStore().read(file_resource_id(live))
    assert claim is not None
    assert claim.fingerprint == observe_file(live).fingerprint

    managed = _install(config, "--auto-accept-tracked")

    assert managed.exit_code == 0, managed.output
    assert live.read_text(encoding="utf-8") == (
        f"home={(tmp_path / 'home').resolve()}\n"
    )
    refreshed = OwnershipStore().read(file_resource_id(live))
    assert refreshed is not None
    assert refreshed.generation > claim.generation
    assert refreshed.fingerprint == observe_file(live).fingerprint


def test_generated_output_transfer_is_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "live" / "generated.txt"
    live.parent.mkdir()
    live.write_text("external\n", encoding="utf-8")
    config_a = _write_generated_repo(tmp_path / "repo-a", live)
    assert _install(config_a).exit_code == 0
    store = OwnershipStore()
    before = store.read(file_resource_id(live))
    assert before is not None

    config_b = _write_generated_repo(tmp_path / "repo-b", live)
    result = _install(config_b)

    assert result.exit_code == 0, result.output
    assert "transferred tracked file ownership" in result.output
    assert live.read_text(encoding="utf-8") == "external\n"
    after = store.read(file_resource_id(live))
    assert after is not None
    assert after.owner_id == read_owner_id(config_b.parent)
    assert after.generation == before.generation + 1


def test_install_refuses_changed_host_input_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    live = tmp_path / "live" / "generated.txt"
    config = _write_generated_repo(tmp_path / "repo", live)
    from setforge import generated as generated_mod

    calls = 0

    def changing_input(_kind: HostInputKind) -> str:
        nonlocal calls
        calls += 1
        return "/frozen" if calls == 1 else "/changed"

    monkeypatch.setattr(generated_mod, "_resolve_input", changing_input)

    result = _install(config)

    assert result.exit_code != 0
    assert "generated host inputs changed after planning" in str(result.exception)
    assert not live.exists()
    assert OwnershipStore().read(file_resource_id(live)) is None
