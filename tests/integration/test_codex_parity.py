"""Mixed Claude/Codex lifecycle contract at the hermetic CLI boundary."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration


def test_mixed_profile_converges_detects_drift_syncs_and_reverts(
    integration_env: Callable[..., IntegrationEnv],
    integration_subprocess,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = integration_env(
        tracked={"claude": ("claude/CLAUDE.md", "# Shared Claude instructions\n")}
    )
    codex_home = env.home / ".codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    codex_source = env.tracked("codex/model.toml")
    codex_source.parent.mkdir(parents=True)
    codex_source.write_text('model = "gpt-5"\n', encoding="utf-8")
    env.config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.4'\n"
        "version: 1\n"
        "tracked_files:\n"
        "  claude:\n"
        "    src: claude/CLAUDE.md\n"
        "    dst: ~/.claude/CLAUDE.md\n"
        "codex:\n"
        "  config:\n"
        "    model: {source: codex/model.toml}\n"
        "profiles:\n"
        "  it:\n"
        "    tracked_files: [claude]\n"
        "    codex:\n"
        "      config: [model]\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(env.repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(env.repo), "commit", "-qm", "codex parity fixture"],
        check=True,
    )

    assert env.run_verb(["validate"]).exit_code == 0
    assert env.run_verb(["lock"]).exit_code == 0
    assert env.run_verb(["compare", "--check"]).exit_code == 1

    install_args = ["install", "--yes", "--no-secrets-scan", "--no-git-check"]
    installed = env.run_verb(install_args)
    assert installed.exit_code == 0, installed.output
    assert env.live(".claude/CLAUDE.md").read_text() == "# Shared Claude instructions\n"
    native = codex_home / "config.toml"
    assert native.read_text() == 'model = "gpt-5"\n'
    assert env.run_verb(["compare", "--check"]).exit_code == 0

    before = native.read_bytes(), env.live(".claude/CLAUDE.md").read_bytes()
    repeated = env.run_verb(install_args)
    assert repeated.exit_code == 0, repeated.output
    assert (native.read_bytes(), env.live(".claude/CLAUDE.md").read_bytes()) == before

    native.write_text('model = "gpt-6"\n', encoding="utf-8")
    assert env.run_verb(["compare", "--check"]).exit_code == 1
    synced = env.run_verb(["sync", "--auto=use-live", "--yes"])
    assert synced.exit_code == 0, synced.output
    assert codex_source.read_text() == 'model = "gpt-6"\n'

    reverted = env.run_verb(["revert", "--yes"])
    assert reverted.exit_code == 0, reverted.output
    assert codex_source.read_text() == 'model = "gpt-5"\n'
