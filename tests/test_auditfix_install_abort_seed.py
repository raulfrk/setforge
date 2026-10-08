"""Regression tests: an aborted install must not seed a host-local store unit.

The section-template seed records a LOCAL reconcile-store unit UNDER the
profile lock, AFTER deploy (STAGE B: it writes the reconcile store,
never ``local.yaml``). Every refuse-before-write gate — validate-srcs,
the unexpected-drift reject, and the secrets-scan abort — fires BEFORE
deploy, so an abort must leave the reconcile store with NO seeded unit.

These tests drive each abort gate and assert the profile's reconcile store
projects no host-local section — neither seeded nor half-written.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app
from setforge.reconcile.host_local_view import host_local_headings_from_store
from setforge.reconcile.types import file_id
from setforge.secrets import SecretAction, SecretFinding, SecretsScanResult
from tests.shared_fixtures import ConfigRepo

_PROFILE = "seed-test"

_DOC = """\
# Title

## Notes

upstream notes body
"""

_TEMPLATE_BODY = "## Python conventions\n\nSEEDED PYTHON CONVENTIONS\n"


@pytest.fixture
def repo(config_repo: ConfigRepo) -> ConfigRepo:
    config_repo.write_tracked("doc.md", _DOC)
    templates = config_repo.root / "templates"
    templates.mkdir(parents=True)
    (templates / "py-conv.md").write_text(_TEMPLATE_BODY, encoding="utf-8")
    return config_repo


def _write_config(repo: ConfigRepo, *, src: str = "doc.md") -> Path:
    return repo.write_config(
        profile=_PROFILE,
        tracked_files={"doc": {"src": src, "dst": "~/.setforge_seed/doc.md"}},
        extra={"section_templates": {"py-conv": {"src": "py-conv.md"}}},
        profile_extra={
            "bootstrap": ["~/.setforge_seed/bootstrap.txt"],
            "section_slots": {"python-conventions": "py-conv"},
        },
    )


def _seeded_in_store() -> bool:
    """True when the reconcile store holds a host-local section for ``doc``."""
    return bool(host_local_headings_from_store(_PROFILE, file_id("doc")))


def _finding() -> SecretFinding:
    return SecretFinding(
        rule_id="generic-api-key",
        file_path=Path("tracked/doc.md"),
        line_number=1,
        snippet="AKIA0000000000000000",  # gitleaks:allow
        snippet_hash="a" * 64,
        secret_kind="aws-key",
    )


def _invoke(config: Path) -> Result:
    """Bare install (no --no-secrets-scan / no --auto): reaches every gate."""
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-git-check",
            "--yes",
            "--no-transition",
        ],
    )


def test_secrets_abort_leaves_store_unseeded(
    repo: ConfigRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secrets-scan abort fires BEFORE deploy+seed — the store must stay
    unseeded."""
    config = _write_config(repo)

    monkeypatch.setattr(
        "setforge.cli.install.secrets_mod.run_pre_deploy_scan",
        lambda **_kw: SecretsScanResult(findings=(_finding(),), files_scanned=1),
    )
    monkeypatch.setattr(
        "setforge.cli.install._plan_secret_findings", lambda *_a, **_kw: None
    )
    mutations: list[str] = []
    monkeypatch.setattr(
        "setforge.cli.install.deploy.bootstrap_local",
        lambda _paths: mutations.append("bootstrap"),
    )
    monkeypatch.setattr(
        "setforge.cli.install.reconcile_packages",
        lambda *_args, **_kwargs: mutations.append("packages"),
    )

    result = _invoke(config)
    assert result.exit_code != 0, result.output
    assert "aborted by secrets scan" in result.output
    assert not _seeded_in_store(), "a secrets abort must not seed the store"
    assert mutations == []


def test_secret_allowlist_decision_is_deferred_and_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planning prompts once per hash; only apply reaches the write sink."""
    from setforge.cli import install as install_mod

    finding = _finding()
    scan = SecretsScanResult(findings=(finding, finding), files_scanned=1)
    allowlist = tmp_path / "allowlist"
    writes: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        install_mod,
        "prompt_secret_action",
        lambda *_args, **_kwargs: SecretAction.ALLOWLIST,
    )
    monkeypatch.setattr(
        install_mod.secrets_mod,
        "append_to_allowlist",
        lambda *, snippet_hash, allowlist_path: writes.append(
            (snippet_hash, allowlist_path)
        ),
    )

    plan = install_mod._plan_secret_findings(scan, yes=True, allowlist_path=allowlist)

    assert plan is not None
    assert plan.hashes == (finding.snippet_hash,)
    assert writes == []
    install_mod._apply_secret_plan(plan)
    assert writes == [(finding.snippet_hash, allowlist)]


def test_secret_silence_decision_plans_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from setforge.cli import install as install_mod

    monkeypatch.setattr(
        install_mod,
        "prompt_secret_action",
        lambda *_args, **_kwargs: SecretAction.SILENCE_ONE_SHOT,
    )
    plan = install_mod._plan_secret_findings(
        SecretsScanResult(findings=(_finding(),), files_scanned=1),
        yes=True,
        allowlist_path=tmp_path / "allowlist",
    )

    assert plan is not None
    assert plan.hashes == ()


def test_drift_gate_abort_leaves_store_unseeded(
    repo: ConfigRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unexpected-drift reject fires BEFORE deploy+seed — the store must
    stay unseeded."""
    config = _write_config(repo)

    def _reject(**_kwargs: object) -> None:
        raise typer.Exit(code=2)

    monkeypatch.setattr("setforge.cli.install._run_predeploy_gates", _reject)

    result = _invoke(config)
    assert result.exit_code != 0
    assert not _seeded_in_store(), "a drift-gate abort must not seed the store"


def test_validate_srcs_abort_leaves_store_unseeded(repo: ConfigRepo) -> None:
    """A profile referencing a missing tracked src aborts at
    validate_srcs_exist — the store must stay unseeded."""
    config = _write_config(repo, src="does-not-exist.md")

    result = _invoke(config)
    assert result.exit_code != 0, result.output
    assert not _seeded_in_store(), "a missing-src abort must not seed the store"
