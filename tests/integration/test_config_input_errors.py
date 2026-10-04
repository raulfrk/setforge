"""Bad config inputs surface as clean errors naming the right file."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from setforge.errors import SetforgeError

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("verb", ["compare", "status", "fetch"])
def test_bad_local_source_block_names_local_yaml(
    integration_env: Callable[..., IntegrationEnv], verb: str
) -> None:
    env = integration_env()
    env.local_config.parent.mkdir(parents=True, exist_ok=True)
    env.local_config.write_text("source:\n  kind: path\n", encoding="utf-8")

    result = env.run_verb(
        [verb], inject_config=verb != "fetch", inject_profile=verb != "fetch"
    )

    assert isinstance(result.exception, SetforgeError), result.output
    message = str(result.exception)
    assert str(env.local_config) in message
    assert "source.path" in message
    assert "SCHEMA VALIDATION ERROR" not in result.output


@pytest.mark.parametrize("verb", ["status", "compare", "validate"])
def test_config_path_that_is_a_directory_is_a_clean_error(
    integration_env: Callable[..., IntegrationEnv], verb: str
) -> None:
    env = integration_env()

    result = env.run_verb(
        [verb, f"--config={env.repo}"],
        inject_config=False,
        inject_profile=verb != "validate",
    )

    assert isinstance(result.exception, SetforgeError), result.output
    assert "is a directory" in str(result.exception)


def test_validate_all_reports_unreadable_tracked_source(
    integration_env: Callable[..., IntegrationEnv],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = integration_env()
    src = env.tracked("text/note.txt")
    src.chmod(0)
    try:
        result = env.run_verb(["validate", "--all"], inject_profile=False)
    finally:
        src.chmod(0o600)

    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "validation FAILED" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "show", "--local"],
        ["config", "add", "--local", "--yes", "binaries.code", "/bin/true"],
        ["config", "remove", "--local", "--yes", "binaries.code"],
    ],
)
def test_config_commands_report_corrupt_local_yaml_cleanly(
    integration_env: Callable[..., IntegrationEnv],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    env = integration_env()
    env.local_config.parent.mkdir(parents=True, exist_ok=True)
    env.local_config.write_text("binaries: [\n", encoding="utf-8")

    result = env.run_verb(argv, inject_config=False, inject_profile=False)

    assert isinstance(result.exception, SetforgeError), result.output
    assert str(env.local_config) in str(result.exception)
