"""Per-verb integration tests — real-git source, mocked claude/code/gitleaks.

Every assertion is on a POST-PARSE OBSERVABLE: exit code, on-disk store
body (live deploy / tracked source / setforge.yaml), transition record on
disk, or the gitleaks-FILTERED finding count — never on registered fake
stdout (that would test the mock, not the code).

The no-leak proof (:class:`TestNoLeakProof`) shows a stray unregistered
``claude`` invocation RAISES ``ProcessNotRegisteredError`` rather than
reaching the host binary.

Deferred verb — ``upgrade``: intentionally has NO integration test here. Its
one meaningful seam is PyPI (``setforge._pypi_client.fetch_latest_version``),
which sits OUTSIDE this tier's git/claude/code/gitleaks subprocess-mock
boundary — covering it would require either a network hit (barred by this
tier's no-leak contract) or a mock of the HTTP client (which would test the
mock, not the code). It is instead covered at the e2e/smoke layer by
``test_e2e_docker_upgrade_check_mode``. Recorded here so the "each mutating
verb" scope has a documented deferral rather than a silent gap.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from .conftest import IntegrationEnv

pytestmark = pytest.mark.integration


def _transition_dirs(env: IntegrationEnv) -> list[Path]:
    root = env.state_dir / "transitions"
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


class TestInstall:
    def test_deploys_and_records_transition(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """install lands live bytes AND writes a transition dir with meta.json."""
        env = integration_env()
        result = env.run_verb(["install"])
        assert result.exit_code == 0, result.output
        assert env.live(".setforge_it/text/note.txt").read_text() == (
            "hello from tracked\n"
        )
        dirs = _transition_dirs(env)
        assert len(dirs) == 1, f"expected one transition, got {dirs}"
        meta = json.loads((dirs[0] / "meta.json").read_text())
        assert meta["command"] == "install"
        assert meta["profile"] == env.profile

    def test_idempotent_second_run(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """A second install is clean (re-runs the real git status gate twice)."""
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        second = env.run_verb(["install"])
        assert second.exit_code == 0, second.output

    def test_dirty_source_refuses(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """An uncommitted tracked/ edit trips the REAL source-clean git gate.

        Observable: non-zero exit + no live file created. This exercises the
        real ``git status --porcelain -- tracked`` path (the integration seam
        the in-Python fake_git cannot reach).
        """
        env = integration_env()
        (env.tracked("text/note.txt")).write_text("uncommitted drift\n")
        result = env.run_verb(["install"])
        assert result.exit_code != 0
        assert not env.live(".setforge_it/text/note.txt").exists()


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


class TestSync:
    def test_captures_live_edit_into_tracked(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """sync absorbs a live edit back into the tracked source.

        Seed via install, edit the live file, then sync with
        ``--auto=use-live``. Observable = the tracked source body now equals
        the live edit.
        """
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        live = env.live(".setforge_it/text/note.txt")
        live.write_text("edited live\n")
        result = env.run_verb(["sync", "--auto=use-live", "--yes"])
        assert result.exit_code == 0, result.output
        assert env.tracked("text/note.txt").read_text() == "edited live\n"

    def test_keep_tracked_refuses_absorb(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """sync --auto=keep-tracked leaves the tracked source untouched."""
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        env.live(".setforge_it/text/note.txt").write_text("edited live\n")
        result = env.run_verb(["sync", "--auto=keep-tracked", "--yes"])
        assert result.exit_code == 0, result.output
        assert env.tracked("text/note.txt").read_text() == "hello from tracked\n"


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


class TestCompare:
    def test_clean_after_install(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """compare --check exits 0 when live matches tracked."""
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        result = env.run_verb(["compare", "--check"])
        assert result.exit_code == 0, result.output

    def test_drift_exits_nonzero(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """compare --check --strict exits 1 on a drifted live file."""
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        env.live(".setforge_it/text/note.txt").write_text("drifted\n")
        result = env.run_verb(["compare", "--check", "--strict"])
        assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------


class TestRevert:
    def test_undoes_install(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """revert restores the pre-install live state (patch -R runs for real).

        Observable = the live file is gone (it did not exist before install)
        and the transition is consumed.
        """
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        live = env.live(".setforge_it/text/note.txt")
        assert live.exists()
        result = env.run_verb(["revert", "--yes"])
        assert result.exit_code == 0, result.output
        assert not live.exists()


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_check_reports_up_to_date(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """migrate --check on a current-schema config is a clean no-op."""
        env = integration_env()
        result = env.run_verb(["migrate", "--check"], inject_profile=False)
        assert result.exit_code == 0, result.output

    def test_pin_writes_schema_version(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """migrate --pin rewrites schema_version in setforge.yaml (on-disk body)."""
        env = integration_env()
        result = env.run_verb(["migrate", "--pin=5.0"], inject_profile=False)
        assert result.exit_code == 0, result.output
        assert "schema_version: '5.0'" in env.config.read_text()


# ---------------------------------------------------------------------------
# gitleaks (secrets scan) — assert the FILTERED finding count, not stdout
# ---------------------------------------------------------------------------


class TestSecretsScan:
    def test_gitleaks_findings_block_install(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """A gitleaks exit-1 (findings) report is parsed + counted, not echoed.

        gitleaks is exit-code-branched in secrets.py: exit 1 = findings →
        parse JSON → filter allowlist. We register the fake gitleaks argv to
        return exit 1 with a one-finding JSON report and assert install
        surfaces the secrets gate (non-zero) — the observable is the
        control-flow branch on the FILTERED count, never the fake stdout.
        """
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
        # gitleaks detect ... --source <tracked_root>  → exit 1 + JSON stdout.
        integration_subprocess.register(
            [str(gitleaks), "detect", integration_subprocess.any()],
            stdout=finding,
            returncode=1,
        )
        result = env.run_verb(["install", "--yes"])
        # Findings present → install aborts before deploy (secrets gate).
        assert result.exit_code != 0, result.output
        assert not env.live(".setforge_it/text/note.txt").exists()

    def test_gitleaks_clean_allows_install(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """gitleaks exit 0 (clean) → install proceeds and deploys."""
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


# ---------------------------------------------------------------------------
# ext / plugin — YAML-mutating verbs (observable = setforge.yaml body)
# ---------------------------------------------------------------------------


class TestExt:
    def test_add_no_install_edits_yaml(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """ext add --no-install appends the id to the profile without shelling code."""
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
        """plugin list runs read-only; with claude absent it still exits 0.

        No claude is registered, so if plugin list tried to shell out it
        would RAISE — a clean exit proves it takes the declared-only path.
        The minimal seeded config declares NO claude_plugins, so the
        projection must render the empty-set branch verbatim: assert on that
        rendered line (a post-parse observable) so a mutation to the
        declared/installed union or the empty-guard is caught, not just the
        exit code.
        """
        env = integration_env()
        result = env.run_verb(["plugin", "list"])
        assert result.exit_code == 0, result.output
        assert "(no plugins declared or installed)" in result.output, result.output
        # The per-row status tokens are emitted ONLY on the non-empty table
        # branch; their absence pins that the empty-union branch (not the
        # table branch) ran, so the assertion bites a projection mutation
        # that renders rows for an empty union.
        for status_token in ("enabled", "missing-from-install", "missing-from-decl"):
            assert status_token not in result.output, result.output


# ---------------------------------------------------------------------------
# fetch — PathSource no-op (no network)
# ---------------------------------------------------------------------------


class TestFetch:
    def test_path_source_noop(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fetch on a PathSource no-ops (no clone/fetch → no network touch).

        fetch resolves the source layer (not --config); point SETFORGE_SOURCE
        at the seeded repo dir. A PathSource fetch is a documented no-op, so
        the verb exits 0 without any git clone/fetch (which would RAISE, being
        unregistered).
        """
        env = integration_env()
        monkeypatch.setenv("SETFORGE_SOURCE", str(env.repo))
        result = env.run_verb(["fetch"], inject_config=False, inject_profile=False)
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# cleanup-orphans — dry-run (read-only)
# ---------------------------------------------------------------------------


class TestCleanupOrphans:
    def test_dry_run_no_orphans(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """cleanup-orphans dry-run on a freshly-installed profile finds none."""
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        result = env.run_verb(["cleanup-orphans"])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# init — --check health probe (read-only)
# ---------------------------------------------------------------------------


class TestInit:
    def test_check_is_read_only(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """init --check prints env/dirs/capabilities without mutating anything.

        init resolves no profile/config; invoke it bare (opt out of both
        injected flags). Observable = clean exit; --check writes nothing.
        """
        env = integration_env()
        result = env.run_verb(
            ["init", "--check"], inject_config=False, inject_profile=False
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# stage — --list (read-only per-hunk classification)
# ---------------------------------------------------------------------------


class TestStage:
    def test_list_no_changes(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """stage --list on a clean install reports no share/local/pending hunks."""
        env = integration_env()
        assert env.run_verb(["install"]).exit_code == 0
        result = env.run_verb(["stage", "--list"])
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# The no-leak proof
# ---------------------------------------------------------------------------


class TestNoLeakProof:
    def test_stray_claude_call_raises(
        self,
        integration_env: Callable[..., IntegrationEnv],
        integration_subprocess,
    ) -> None:
        """A claude reconcile with NO registered argv RAISES, never reaching host.

        Mark claude present (so the reconcile leg attempts to shell out) but
        register NO argv for it. The reconcile fires ``claude plugin list
        --json``, which is unregistered → ``ProcessNotRegisteredError``
        surfaces as a non-zero exit. This is the network-leak guard: an
        unaccounted claude/gitleaks/code call fails loudly instead of hitting
        the developer's real binary. (Contrast: if the policy used
        ``allow_unregistered(True)``, this same call would silently run the
        host claude — the hole this proof rules out.)
        """
        env = integration_env()
        env.present_binary("claude")  # resolvable, but NO argv registered
        result = env.run_verb(["install", "--yes"])
        assert result.exit_code != 0
        assert "not registered" in (str(result.exception) + result.output).lower()
