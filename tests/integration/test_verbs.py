"""Per-verb integration tests — real-git source, mocked claude/code/gitleaks.
Every assertion is a POST-PARSE OBSERVABLE (exit code / on-disk body /
transition record / filtered finding count), never registered fake stdout.
``upgrade`` has no test here: its only seam is PyPI, outside this tier's
no-network contract; covered instead by test_e2e_docker_upgrade_check_mode."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration


def _transition_dirs(env: IntegrationEnv) -> list[Path]:
    root = env.state_dir / "transitions"
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _latest_transition_meta(env: IntegrationEnv) -> dict[str, object]:
    dirs = _transition_dirs(env)
    assert dirs, "expected at least one transition"
    return json.loads((dirs[-1] / "meta.json").read_text())


def _assert_transition_command(meta: dict[str, object], command: str) -> None:
    """Pin the command metadata at the fast CLI/filesystem boundary."""
    assert meta["command"] == command
    assert isinstance(meta.get("end_timestamp"), str)
    command_line = meta.get("command_line")
    # CliRunner stays in the pytest process, so the captured argv is pytest's;
    # xdist workers may expose an empty argv. The dedicated redaction test below
    # injects a non-empty value, while Docker pins real ``setforge`` argv.
    assert isinstance(command_line, list)
    assert "preserve_user_keys_applied" not in meta


class TestInstall:
    def test_deploys_and_records_transition(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        result = env.run_verb(["install"])
        assert result.exit_code == 0, result.output
        assert env.live(".setforge_it/text/note.txt").read_text() == (
            "hello from tracked\n"
        )
        dirs = _transition_dirs(env)
        assert len(dirs) == 1, f"expected one transition, got {dirs}"
        meta = json.loads((dirs[0] / "meta.json").read_text())
        _assert_transition_command(meta, "install")
        assert meta["profile"] == env.profile

    def test_persisted_command_line_redacts_secret_argv(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The install transition writer applies redaction before persistence."""
        env = integration_env()
        secret = "ghp_DO_NOT_PERSIST"
        monkeypatch.setattr(
            "setforge.cli._install_helpers.sys",
            SimpleNamespace(argv=["setforge", "install", f"--token={secret}"]),
        )
        result = env.run_verb(["install"])
        assert result.exit_code == 0, result.output
        command_line = _latest_transition_meta(env)["command_line"]
        assert isinstance(command_line, list)
        rendered = " ".join(str(arg) for arg in command_line)
        assert secret not in rendered
        assert "--token=<REDACTED>" in rendered

    def test_idempotent_second_run(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        second = env.run_verb(["install"])
        assert second.exit_code == 0, second.output

    def test_dirty_source_refuses(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        (env.tracked("text/note.txt")).write_text("uncommitted drift\n")
        result = env.run_verb(["install"])
        assert result.exit_code != 0
        assert not env.live(".setforge_it/text/note.txt").exists()


class TestSync:
    def test_captures_live_edit_into_tracked(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        live = env.live(".setforge_it/text/note.txt")
        live.write_text("edited live\n")
        result = env.run_verb(["sync", "--auto=use-live", "--yes"])
        assert result.exit_code == 0, result.output
        assert env.tracked("text/note.txt").read_text() == "edited live\n"
        _assert_transition_command(_latest_transition_meta(env), "sync")

    def test_keep_tracked_refuses_absorb(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        env.live(".setforge_it/text/note.txt").write_text("edited live\n")
        result = env.run_verb(["sync", "--auto=keep-tracked", "--yes"])
        assert result.exit_code == 0, result.output
        assert env.tracked("text/note.txt").read_text() == "hello from tracked\n"


class TestCompare:
    def test_clean_after_install(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        result = env.run_verb(["compare", "--check"])
        assert result.exit_code == 0, result.output

    def test_drift_exits_nonzero(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        env.live(".setforge_it/text/note.txt").write_text("drifted\n")
        result = env.run_verb(["compare", "--check", "--strict"])
        assert result.exit_code == 1, result.output


class TestRevert:
    def test_undoes_install(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        live = env.live(".setforge_it/text/note.txt")
        assert live.exists()
        result = env.run_verb(["revert", "--yes"])
        assert result.exit_code == 0, result.output
        assert not live.exists()
        _assert_transition_command(_latest_transition_meta(env), "revert")


class TestMigrate:
    def test_check_reports_up_to_date(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        result = env.run_verb(["migrate", "--check"], inject_profile=False)
        assert result.exit_code == 0, result.output

    def test_pin_writes_schema_version(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        result = env.run_verb(["migrate", "--pin=5.0"], inject_profile=False)
        assert result.exit_code == 0, result.output
        assert "schema_version: '5.0'" in env.config.read_text()


class TestSecretsScan:
    def test_gitleaks_findings_block_install(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        gitleaks = env.present_binary("gitleaks")
        finding = json.dumps(
            [
                {
                    "RuleID": "generic-api-key",
                    "File": "text/note.txt",
                    "StartLine": 1,
                    "Match": "api_key = AKIA................",
                    "Secret": "AKIA................",
                }
            ]
        )
        integration_subprocess.register(
            [str(gitleaks), "detect", integration_subprocess.any()],
            stdout=finding,
            returncode=1,
        )
        result = env.run_verb(["install", "--yes"])
        assert result.exit_code != 0, result.output
        assert not env.live(".setforge_it/text/note.txt").exists()

    def test_gitleaks_clean_allows_install(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        gitleaks = env.present_binary("gitleaks")
        integration_subprocess.register(
            [str(gitleaks), "detect", integration_subprocess.any()],
            stdout="",
            returncode=0,
        )
        result = env.run_verb(["install"])
        assert result.exit_code == 0, result.output
        assert env.live(".setforge_it/text/note.txt").exists()


class TestExt:
    def test_add_no_install_edits_yaml(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        result = env.run_verb(["ext", "add", "ms-python.python", "--no-install"])
        assert result.exit_code == 0, result.output
        assert "ms-python.python" in env.config.read_text()


class TestPlugin:
    def test_list_reports_without_claude(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        result = env.run_verb(["plugin", "list"])
        assert result.exit_code == 0, result.output
        assert "(no plugins declared or installed)" in result.output, result.output
        # Absence of per-row tokens pins the empty-union branch, not the table branch.
        for status_token in ("enabled", "missing-from-install", "missing-from-decl"):
            assert status_token not in result.output, result.output


class TestFetch:
    def test_path_source_noop(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = integration_env()
        monkeypatch.setenv("SETFORGE_SOURCE", str(env.repo))
        result = env.run_verb(["fetch"], inject_config=False, inject_profile=False)
        assert result.exit_code == 0, result.output


class TestCleanupOrphans:
    def test_dry_run_no_orphans(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        result = env.run_verb(["cleanup-orphans"])
        assert result.exit_code == 0, result.output


class TestInit:
    def test_check_is_read_only(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        result = env.run_verb(
            ["init", "--check"], inject_config=False, inject_profile=False
        )
        assert result.exit_code == 0, result.output


class TestStage:
    def test_list_no_changes(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        result = env.run_verb(["stage", "--list"])
        assert result.exit_code == 0, result.output


class TestNoLeakProof:
    def test_stray_claude_call_raises(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        env = integration_env()
        claude = env.present_binary("claude")
        # ``install`` intentionally skips Claude when a profile declares no
        # plugins. ``plugin list`` always probes the present binary, making it
        # the direct proof that an unregistered subprocess cannot reach host.
        result = env.run_verb(["plugin", "list"])
        error = str(result.exception) + result.output
        assert result.exit_code != 0
        assert "not registered" in error.lower()
        assert f"{claude} plugin list --json" in error
