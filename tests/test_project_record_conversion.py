"""Older injection records are converted once, then read as the current format."""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge import operations
from setforge.cli import app
from setforge.ownership import ClaimLifecycle, OwnershipStore
from setforge.project_injection import manifest_path
from tests.project_helpers import _config, _git_repo, _write_older_record


def _run(*arguments: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["project", *arguments])
    return result.exit_code, result.output + str(result.exception or "")


@pytest.fixture
def injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    config = _config(tmp_path)
    target = _git_repo(tmp_path / "target")
    code, output = _run("inject", "demo", str(target), "--config", str(config), "--yes")
    assert code == 0, output
    return config, target


@pytest.mark.parametrize("schema", [1, 2])
def test_read_only_commands_refuse_an_older_record_and_leave_it_alone(
    injected: tuple[Path, Path], schema: int
) -> None:
    config, target = injected
    record = manifest_path(target, "demo")
    before = _write_older_record(record, schema)
    hint = f"run `setforge project sync {target}` to convert it"

    for arguments in (
        ("list",),
        ("sync", str(target), "--dry-run"),
        ("remove", "demo", str(target), "--config", str(config), "--dry-run"),
        ("visibility", str(target), "AGENTS.md", "--tracked", "--dry-run"),
    ):
        code, output = _run(*arguments)
        assert code == 1, (arguments, output)
        assert "project injection record is in an older format" in output
        assert hint in output
        assert record.read_bytes() == before


@pytest.mark.parametrize("schema", [1, 2])
def test_sync_converts_an_older_record_then_list_sync_and_remove_work(
    injected: tuple[Path, Path], schema: int
) -> None:
    config, target = injected
    record = manifest_path(target, "demo")
    current = json.loads(record.read_text())
    _write_older_record(record, schema)

    code, output = _run("sync", str(target), "--yes")

    assert code == 0, output
    assert (
        "converted the injection record of project profile 'demo' to the current "
        "format" in output
    )
    assert "demo: unchanged: AGENTS.md" in output
    assert json.loads(record.read_text()) == current
    assert (target / "AGENTS.md").read_text() == "managed\n"
    code, output = _run("list")
    assert code == 0, output
    assert "hidden: AGENTS.md" in output
    (config.parent / "project/demo/AGENTS.md").write_text("managed again\n")
    code, output = _run("sync", str(target), "--yes")
    assert code == 0, output
    assert "converted" not in output
    assert (target / "AGENTS.md").read_text() == "managed again\n"
    code, output = _run("remove", "demo", str(target), "--config", str(config), "--yes")
    assert code == 0, output
    assert not (target / "AGENTS.md").exists()
    assert not record.exists()
    assert [claim.lifecycle for claim in OwnershipStore().list_claims()] == [
        ClaimLifecycle.RELEASED
    ]


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("command", ["remove", "visibility", "inject"])
def test_every_changing_command_converts_before_it_plans(
    injected: tuple[Path, Path], schema: int, command: str
) -> None:
    config, target = injected
    record = manifest_path(target, "demo")
    _write_older_record(record, schema)
    arguments = {
        "remove": ("remove", "demo", str(target), "--config", str(config)),
        "visibility": ("visibility", str(target), "AGENTS.md", "--tracked"),
        "inject": ("inject", "demo", str(target), "--config", str(config)),
    }[command]

    code, output = _run(*arguments, "--yes")

    assert "converted the injection record of project profile 'demo'" in output
    if command == "remove":
        assert code == 0, output
        assert not record.exists()
        assert not (target / "AGENTS.md").exists()
    else:
        assert json.loads(record.read_text())["schema"] == 3
        assert (target / "AGENTS.md").read_text() == "managed\n"


def test_format_one_content_comes_from_the_profile_source_when_the_file_was_edited(
    injected: tuple[Path, Path],
) -> None:
    _config_path, target = injected
    record = manifest_path(target, "demo")
    _write_older_record(record, 1)
    (target / "AGENTS.md").write_text("managed\nlocal note\n")

    code, output = _run("sync", str(target), "--yes")

    assert code == 0, output
    entry = json.loads(record.read_text())["files"][0]
    assert base64.b64decode(entry["upstream_payload"]) == b"managed\n"
    assert base64.b64decode(entry["applied_payload"]) == b"managed\nlocal note\n"
    assert (target / "AGENTS.md").read_text() == "managed\nlocal note\n"


def test_format_one_record_whose_injected_content_is_gone_is_refused_untouched(
    injected: tuple[Path, Path],
) -> None:
    config, target = injected
    record = manifest_path(target, "demo")
    before = _write_older_record(record, 1)
    (target / "AGENTS.md").write_text("local\n")
    (config.parent / "project/demo/AGENTS.md").write_text("profile\n")

    for arguments in (
        ("sync", str(target), "--auto=use-profile", "--yes"),
        ("remove", "demo", str(target), "--config", str(config), "--yes"),
    ):
        code, output = _run(*arguments)
        assert code == 1, output
        assert "is in an older format and cannot be converted" in output
        assert "SetForge 1.3 or 1.4" in output
        assert record.read_bytes() == before
        assert (target / "AGENTS.md").read_text() == "local\n"


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("mode", "inconsistent file record"),
        ("digest", "cannot be converted"),
        ("extra-field", "invalid fields"),
        ("moved-config", "config cannot be resolved safely"),
    ],
)
def test_malformed_format_one_record_is_refused_untouched(
    injected: tuple[Path, Path], damage: str, message: str
) -> None:
    config, target = injected
    record = manifest_path(target, "demo")
    _write_older_record(record, 1)
    document = json.loads(record.read_text())
    entry = document["files"][0]
    if damage == "mode":
        entry["applied_mode"] = 0o10000
    elif damage == "digest":
        entry["applied_digest"] = "0" * 64
    elif damage == "extra-field":
        entry["upstream_mode"] = 0o644
    else:
        config.rename(config.with_name("moved.yaml"))
    record.write_text(json.dumps(document))
    before = record.read_bytes()

    code, output = _run("sync", str(target), "--yes")

    assert code == 1, output
    assert message in output
    assert record.read_bytes() == before


@pytest.mark.parametrize("schema", [1, 2])
def test_interrupted_conversion_is_recovered_to_the_older_record(
    injected: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, schema: int
) -> None:
    _config_path, target = injected
    record = manifest_path(target, "demo")
    before = _write_older_record(record, schema)
    reached: list[str] = []

    def after_write(
        journal: operations.OperationJournal,
    ) -> operations.OperationJournal:
        assert json.loads(record.read_text())["schema"] == 3
        reached.append(journal.profile)
        raise OSError("interrupted after the record was rewritten")

    def no_automatic_recovery(journal: operations.OperationJournal) -> bool:
        raise OSError("interrupted before recovery")

    with monkeypatch.context() as fault:
        fault.setattr(operations, "finish_checkpoint", after_write)
        fault.setattr(operations, "recover_automatically", no_automatic_recovery)
        failed = CliRunner().invoke(app, ["project", "sync", str(target), "--yes"])
    assert isinstance(failed.exception, OSError)
    assert len(reached) == 1
    assert operations.active(reached[0]) is not None

    recovered = CliRunner().invoke(
        app, ["recover", f"--profile={reached[0]}", "--apply", "--yes"]
    )

    assert recovered.exit_code == 0, (recovered.output, recovered.exception)
    assert operations.active(reached[0]) is None
    assert record.read_bytes() == before
    code, output = _run("sync", str(target), "--yes")
    assert code == 0, output
    assert json.loads(record.read_text())["schema"] == 3


@pytest.mark.parametrize("schema", [1, 2])
def test_remove_drops_an_older_record_whose_directory_is_gone(
    injected: tuple[Path, Path], schema: int
) -> None:
    config, target = injected
    record = manifest_path(target, "demo")
    _write_older_record(record, schema)
    shutil.rmtree(target)

    code, output = _run("remove", "demo", str(target), "--config", str(config), "--yes")

    assert code == 0, output
    assert "stale injection dropped" in output
    assert not record.exists()


@pytest.mark.parametrize(
    "arguments",
    [("list",), ("sync", "{target}", "--yes"), ("inject", "demo", "{target}", "--yes")],
)
def test_older_record_of_a_replaced_directory_names_the_command_that_drops_it(
    injected: tuple[Path, Path], tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    config, target = injected
    record = manifest_path(target, "demo")
    before = _write_older_record(record, 2)
    target.rename(tmp_path / "moved")
    _git_repo(target)
    command = [item.format(target=target) for item in arguments]
    if command[0] == "inject":
        command += ["--config", str(config)]

    code, output = _run(*command)

    assert code == 1, output
    assert "project injection record is in an older format" in output
    assert f"run `setforge project remove demo {target}` to drop it" in output
    assert "project sync" not in output
    assert record.read_bytes() == before
    if command[0] == "list":
        assert f"{target}  [demo]" in output
    code, output = _run("remove", "demo", str(target), "--config", str(config), "--yes")
    assert code == 0, output
    assert "stale injection dropped" in output
    assert not record.exists()


def test_list_names_the_command_that_drops_an_older_record_of_a_missing_directory(
    injected: tuple[Path, Path], tmp_path: Path
) -> None:
    config, target = injected
    record = manifest_path(target, "demo")
    _write_older_record(record, 2)
    target.rename(tmp_path / "moved")

    code, output = _run("list")

    assert code == 1, output
    assert f"{target}  [demo]" in output
    assert "project injection record is in an older format" in output
    assert f"run `setforge project remove demo {target}` to drop it" in output
    assert "project sync" not in output
    code, output = _run("remove", "demo", str(target), "--config", str(config), "--yes")
    assert code == 0, output
    assert "stale injection dropped" in output
    assert not record.exists()
    assert _run("list") == (0, "no project injections recorded\n")


def test_conversion_leaves_records_of_other_directories_alone(
    injected: tuple[Path, Path], tmp_path: Path
) -> None:
    config, target = injected
    other = _git_repo(tmp_path / "other")
    code, output = _run("inject", "demo", str(other), "--config", str(config), "--yes")
    assert code == 0, output
    untouched = _write_older_record(manifest_path(other, "demo"), 2)
    _write_older_record(manifest_path(target, "demo"), 2)

    code, output = _run("sync", str(target), "--yes")

    assert code == 0, output
    assert manifest_path(other, "demo").read_bytes() == untouched
    assert json.loads(manifest_path(target, "demo").read_text())["schema"] == 3
