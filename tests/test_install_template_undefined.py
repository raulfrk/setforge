"""A misspelled destination-template variable stops install before any write.

``validate`` renders a ``template: true`` destination with strict undefined
handling and rejects an unknown variable. ``install`` (and its dry-run) must
refuse the same config rather than collapse the unknown variable to nothing and
deploy to a different path than the one written.

Drives the real ``setforge`` CLI against a temp config repo with a sandboxed
``$HOME`` + ``$SETFORGE_STATE_DIR``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge import transitions
from setforge.cli import app
from setforge.errors import ConfigError
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
