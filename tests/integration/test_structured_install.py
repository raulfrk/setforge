"""Install of structured (YAML/JSON) tracked files through the real verbs.

Every assertion is on the bytes on disk and the exit code: a structured file
must come out of ``install`` byte-identical to the side that changed, a second
``install`` must change nothing, and ``compare --check`` must be clean."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration

_INSTALL = ["install", "--yes", "--no-git-check", "--no-secrets-scan"]

_YAML = "# top comment\na: 1\nb: two\nnested:\n  x:\n    - 1\n    - 2\n  y: null\n"
_JSON = '{\n  // note\n  "a": 1,\n  "b": 2\n}\n'


def _env(integration_env: Callable[..., IntegrationEnv]) -> IntegrationEnv:
    return integration_env(
        tracked={
            "conf": ("conf.yaml", _YAML),
            "settings": ("settings.json", _JSON),
        }
    )


def test_second_install_of_unchanged_structured_files_changes_nothing(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = _env(integration_env)
    assert env.run_verb(_INSTALL).exit_code == 0

    second = env.run_verb(_INSTALL)

    assert second.exit_code == 0, second.output
    assert env.live(".setforge_it/conf.yaml").read_bytes() == _YAML.encode()
    assert env.live(".setforge_it/settings.json").read_bytes() == _JSON.encode()
    assert env.run_verb(["compare", "--check"]).exit_code == 0


def test_tracked_update_reaches_untouched_live_byte_for_byte(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = _env(integration_env)
    assert env.run_verb(_INSTALL).exit_code == 0
    new_yaml = _YAML.replace("a: 1", "a: 9")
    new_json = _JSON.replace("// note", "// changed note").replace(
        '"b": 2', '"b": 2,\n  "c": 3'
    )
    env.tracked("conf.yaml").write_text(new_yaml, encoding="utf-8")
    env.tracked("settings.json").write_text(new_json, encoding="utf-8")

    updated = env.run_verb(_INSTALL)

    assert updated.exit_code == 0, updated.output
    assert env.live(".setforge_it/conf.yaml").read_bytes() == new_yaml.encode()
    assert env.live(".setforge_it/settings.json").read_bytes() == new_json.encode()
    assert env.run_verb(["compare", "--check"]).exit_code == 0
    assert env.run_verb(_INSTALL).exit_code == 0
    assert env.live(".setforge_it/conf.yaml").read_bytes() == new_yaml.encode()


def test_install_leaves_host_edit_alone_when_tracked_did_not_change(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = _env(integration_env)
    assert env.run_verb(_INSTALL).exit_code == 0
    edited = _YAML.replace("b: two", "b: mine  # host")
    env.live(".setforge_it/conf.yaml").write_text(edited, encoding="utf-8")

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 0, result.output
    assert env.live(".setforge_it/conf.yaml").read_bytes() == edited.encode()
