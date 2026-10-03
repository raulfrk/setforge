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


_TRUNCATED = '{\n  "a": 1,\n  "L'


def _env_with_note(integration_env: Callable[..., IntegrationEnv]) -> IntegrationEnv:
    return integration_env(
        tracked={
            "settings": ("settings.json", _JSON),
            "note": ("note.txt", "one\n"),
        }
    )


def test_unparseable_live_json_does_not_block_other_files(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = _env_with_note(integration_env)
    assert env.run_verb(_INSTALL).exit_code == 0
    env.live(".setforge_it/settings.json").write_text(_TRUNCATED, encoding="utf-8")
    env.tracked("note.txt").write_text("two\n", encoding="utf-8")

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 0, result.output
    assert env.live(".setforge_it/note.txt").read_text(encoding="utf-8") == "two\n"
    assert env.live(".setforge_it/settings.json").read_bytes() == _TRUNCATED.encode()


def test_use_tracked_restores_unparseable_live_json(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = _env_with_note(integration_env)
    assert env.run_verb(_INSTALL).exit_code == 0
    env.live(".setforge_it/settings.json").write_text(_TRUNCATED, encoding="utf-8")
    env.tracked("note.txt").write_text("two\n", encoding="utf-8")

    result = env.run_verb([*_INSTALL, "--auto=use-tracked"])

    assert result.exit_code == 0, result.output
    assert env.live(".setforge_it/note.txt").read_text(encoding="utf-8") == "two\n"
    assert env.live(".setforge_it/settings.json").read_bytes() == _JSON.encode()
    assert env.run_verb(["compare", "--check"]).exit_code == 0


def test_unparseable_live_json_conflict_names_the_file(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    env = _env_with_note(integration_env)
    assert env.run_verb(_INSTALL).exit_code == 0
    env.live(".setforge_it/settings.json").write_text(_TRUNCATED, encoding="utf-8")
    env.tracked("settings.json").write_text(
        _JSON.replace('"a": 1', '"a": 2'), encoding="utf-8"
    )
    env.tracked("note.txt").write_text("two\n", encoding="utf-8")

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 1, result.output
    assert "settings.json" in result.output.replace("\n", "")
    assert env.live(".setforge_it/note.txt").read_text(encoding="utf-8") == "two\n"
    assert env.live(".setforge_it/settings.json").read_bytes() == _TRUNCATED.encode()


def test_multi_document_yaml_update_installs(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    base = "---\nkind: A\ndata: 1\n---\nkind: B\ndata: 2\n"
    env = integration_env(tracked={"conf": ("conf.yaml", base)})
    assert env.run_verb(_INSTALL).exit_code == 0
    live = env.live(".setforge_it/conf.yaml")
    live.write_text(base.replace("data: 1", "data: 7"), encoding="utf-8")
    env.tracked("conf.yaml").write_text(base.replace("data: 2", "data: 3"))

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 0, result.output
    assert live.read_bytes() == b"---\nkind: A\ndata: 7\n---\nkind: B\ndata: 3\n"
    assert env.run_verb(_INSTALL).exit_code == 0
    assert live.read_bytes() == b"---\nkind: A\ndata: 7\n---\nkind: B\ndata: 3\n"


def test_duplicate_key_live_json_does_not_block_install(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
) -> None:
    base = '{\n  "a": 1,\n  "m": 0,\n  "z": 1\n}\n'
    env = integration_env(
        tracked={"settings": ("settings.json", base), "note": ("note.txt", "one\n")}
    )
    assert env.run_verb(_INSTALL).exit_code == 0
    live = env.live(".setforge_it/settings.json")
    live.write_text(base.replace('"a": 1,', '"a": 1,\n  "a": 5,'), encoding="utf-8")
    env.tracked("settings.json").write_text(base.replace('"z": 1', '"z": 2'))
    env.tracked("note.txt").write_text("two\n", encoding="utf-8")

    result = env.run_verb(_INSTALL)

    assert result.exit_code == 0, result.output
    assert env.live(".setforge_it/note.txt").read_text(encoding="utf-8") == "two\n"
    assert live.read_bytes() == b'{\n  "a": 1,\n  "a": 5,\n  "m": 0,\n  "z": 2\n}\n'
    assert env.run_verb(["stage", "--list"]).exit_code == 0
