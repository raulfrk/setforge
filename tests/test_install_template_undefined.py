"""A misspelled destination-template variable stops install before any write.

``validate`` renders a ``template: true`` destination with strict undefined
handling and rejects an unknown variable. ``install`` (and its dry-run) must
refuse the same config rather than collapse the unknown variable to nothing and
deploy to a different path than the one written.

Drives the real ``setforge`` CLI against a temp config repo with a sandboxed
``$HOME`` + ``$SETFORGE_STATE_DIR``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import transitions
from setforge.cli import app
from setforge.errors import ConfigError
from setforge.file_ownership import file_resource_id
from setforge.ownership import OwnershipStore
from tests.shared_fixtures import ConfigRepo

_PROFILE = "test-template"
_BODY = "tracked body\n"


def _write_config(config_repo: ConfigRepo, dst: str) -> Path:
    config_repo.write_tracked("wrong.txt", _BODY)
    return config_repo.write_config(
        profile=_PROFILE,
        tracked_files={"t": {"src": "wrong.txt", "dst": dst, "template": True}},
    )


def _run(verb: str, config: Path, *extra: str) -> Result:
    args = [verb, f"--profile={_PROFILE}", f"--config={config}", *extra]
    return CliRunner().invoke(app, args)


def _install(config: Path, *extra: str) -> Result:
    return _run(
        "install", config, "--no-secrets-scan", "--no-git-check", "--yes", *extra
    )


def _transition_count() -> int:
    root = transitions.transitions_root()
    if not root.exists():
        return 0
    return sum(1 for entry in root.iterdir() if entry.is_dir())


@pytest.mark.parametrize("extra", [(), ("--dry-run",)], ids=["install", "dry-run"])
def test_install_refuses_undefined_destination_variable_before_writing(
    config_repo: ConfigRepo, extra: tuple[str, ...]
) -> None:
    """A typo'd variable exits non-zero, names the variable, writes nothing."""
    config = _write_config(config_repo, "{{ home }}/{{ home_typo }}/wrong.txt")

    validated = _run("validate", config)
    assert validated.exit_code == 1, validated.output
    assert "home_typo" in validated.output

    result = _install(config, *extra)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, ConfigError)
    assert "home_typo" in str(result.exception)
    assert not (Path.home() / "wrong.txt").exists()
    assert _transition_count() == 0


def test_install_defined_destination_variable_still_deploys(
    config_repo: ConfigRepo,
) -> None:
    """A valid ``{{ home }}`` destination resolves and deploys as before."""
    config = _write_config(config_repo, "{{ home }}/.setforge_template/ok.txt")

    assert _run("validate", config).exit_code == 0

    result = _install(config)

    assert result.exit_code == 0, result.output
    deployed = Path.home() / ".setforge_template" / "ok.txt"
    assert deployed.read_text(encoding="utf-8") == _BODY


def _break_destination(config: Path) -> None:
    """Misspell the destination variable, as a hasty config edit would."""
    text = config.read_text(encoding="utf-8")
    assert "{{ home }}/out" in text
    config.write_text(
        text.replace("{{ home }}/out", "{{ home_typo }}/out"), encoding="utf-8"
    )


def test_revert_still_undoes_install_after_the_destination_is_misspelled(
    config_repo: ConfigRepo,
) -> None:
    """A bad config edit must not trap the user: undo has to keep working."""
    config = _write_config(config_repo, "{{ home }}/out/good.txt")
    deployed = Path.home() / "out" / "good.txt"
    assert _install(config).exit_code == 0
    config_repo.write_tracked("wrong.txt", "second body\n")
    assert _install(config).exit_code == 0
    assert deployed.read_text(encoding="utf-8") == "second body\n"

    _break_destination(config)
    assert _run("validate", config).exit_code == 1

    result = _run("revert", config, "--yes")

    assert result.exit_code == 0, result.output
    assert deployed.read_text(encoding="utf-8") == _BODY

    redo = _run("revert", config, "--yes")

    assert redo.exit_code == 0, redo.output
    assert deployed.read_text(encoding="utf-8") == "second body\n"


_TYPO_DST = "{{ home }}/{{ home_typo }}/bad.txt"


def _write_two_profile_config(config_repo: ConfigRepo, *, with_typo: bool) -> Path:
    """Profile ``p`` uses a valid file; profile ``q`` uses the typo'd one."""
    config_repo.write_tracked("ok.txt", _BODY)
    config_repo.write_tracked("bad.txt", _BODY)
    typo = (
        f"  bad:\n    src: bad.txt\n    dst: {_TYPO_DST!r}\n    template: true\n"
        if with_typo
        else ""
    )
    q = "  q:\n    tracked_files: [bad]\n" if with_typo else ""
    config_repo.config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  good:\n    src: ok.txt\n    dst: '{{ home }}/out/ok.txt'\n"
        "    template: true\n"
        f"{typo}"
        "profiles:\n  p:\n    tracked_files: [good]\n"
        f"{q}",
        encoding="utf-8",
    )
    return config_repo.config


def _run_as(profile: str, verb: str, config: Path, *extra: str) -> Result:
    args = [verb, f"--profile={profile}", f"--config={config}", *extra]
    return CliRunner().invoke(app, args)


def test_typo_in_a_file_another_profile_uses_does_not_break_this_profile(
    config_repo: ConfigRepo,
) -> None:
    config = _write_two_profile_config(config_repo, with_typo=True)
    assert _run_as("p", "validate", config).exit_code == 0
    install_args = ("--no-secrets-scan", "--no-git-check", "--yes")
    assert _run_as("p", "install", config, *install_args).exit_code == 0

    compared = _run_as("p", "compare", config)
    cleaned = _run_as("p", "cleanup-orphans", config)

    assert compared.exit_code == 0, compared.output
    assert cleaned.exit_code == 0, cleaned.output


def test_scan_lists_the_same_files_when_another_profile_has_a_typo(
    config_repo: ConfigRepo,
) -> None:
    config = _write_two_profile_config(config_repo, with_typo=False)
    install_args = ("--no-secrets-scan", "--no-git-check", "--yes")
    assert _run_as("p", "install", config, *install_args).exit_code == 0
    stray = Path.home() / "out" / "stray.txt"
    stray.write_text("not recorded\n", encoding="utf-8")
    baseline = _run_as("p", "cleanup-orphans", config, "--scan")
    assert baseline.exit_code == 0, baseline.output
    assert str(stray) in baseline.output

    _write_two_profile_config(config_repo, with_typo=True)
    result = _run_as("p", "cleanup-orphans", config, "--scan")

    assert result.exit_code == 0, result.output
    assert result.output == baseline.output


@pytest.mark.parametrize(
    ("verb", "extra"),
    [("compare", ()), ("cleanup-orphans", ()), ("cleanup-orphans", ("--scan",))],
    ids=["compare", "cleanup-orphans", "cleanup-orphans-scan"],
)
def test_typo_in_this_profiles_own_file_is_still_refused(
    config_repo: ConfigRepo, verb: str, extra: tuple[str, ...]
) -> None:
    config = _write_two_profile_config(config_repo, with_typo=True)

    result = _run_as("q", verb, config, *extra)

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, ConfigError)
    assert "home_typo" in str(result.exception)


def test_ownership_revert_ignores_a_typo_in_an_unrelated_file(
    config_repo: ConfigRepo, init_git_repo: Callable[[Path], Path]
) -> None:
    """Re-granting a released claim only needs the file the claim names."""
    init_git_repo(config_repo.root)
    config = _write_two_profile_config(config_repo, with_typo=False)
    install_args = ("--no-secrets-scan", "--no-git-check", "--no-fetch", "--yes")
    assert _run_as("p", "install", config, *install_args).exit_code == 0
    claim_id = OwnershipStore().claim_id(
        file_resource_id(Path.home() / "out" / "ok.txt")
    )
    released = CliRunner().invoke(
        app, ["ownership", "release", claim_id, f"--config={config}", "--yes"]
    )
    assert released.exit_code == 0, released.output
    transition_id = released.output.split("released ownership transition ")[1].split()[
        0
    ]
    _write_two_profile_config(config_repo, with_typo=True)

    result = CliRunner().invoke(
        app, ["ownership", "revert", transition_id, f"--config={config}", "--yes"]
    )

    assert result.exit_code == 0, result.output
