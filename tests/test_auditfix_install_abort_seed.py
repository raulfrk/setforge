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
from setforge.reconcile.host_local_view import host_local_sections_from_store
from setforge.secrets import SecretFinding, SecretsScanResult

_PROFILE = "seed-test"

_DOC = """\
# Title

## Notes

upstream notes body
"""

_TEMPLATE_BODY = "## Python conventions\n\nSEEDED PYTHON CONVENTIONS\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    (target / "tracked").mkdir(parents=True)
    (target / "tracked" / "doc.md").write_text(_DOC, encoding="utf-8")
    (target / "templates").mkdir(parents=True)
    (target / "templates" / "py-conv.md").write_text(_TEMPLATE_BODY, encoding="utf-8")
    return target


def _write_config(repo: Path, *, src: str = "doc.md") -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        f"    src: {src}\n"
        "    dst: ~/.setforge_seed/doc.md\n"
        "section_templates:\n"
        "  py-conv:\n"
        "    src: py-conv.md\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - doc\n"
        "    section_slots:\n"
        "      python-conventions: py-conv\n",
        encoding="utf-8",
    )
    return config


def _seeded_in_store() -> bool:
    """True when the profile's reconcile store projects any host-local section."""
    return bool(host_local_sections_from_store(_PROFILE))


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
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secrets-scan abort fires BEFORE deploy+seed — the store must stay
    unseeded."""
    config = _write_config(repo)

    monkeypatch.setattr(
        "setforge.cli.install.secrets_mod.run_pre_deploy_scan",
        lambda **_kw: SecretsScanResult(findings=(_finding(),), files_scanned=1),
    )
    monkeypatch.setattr(
        "setforge.cli.install._handle_secret_findings",
        lambda *_a, **_kw: False,
    )

    result = _invoke(config)
    assert result.exit_code != 0, result.output
    assert "aborted by secrets scan" in result.output
    assert not _seeded_in_store(), "a secrets abort must not seed the store"


def test_drift_gate_abort_leaves_store_unseeded(
    repo: Path, monkeypatch: pytest.MonkeyPatch
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


def test_validate_srcs_abort_leaves_store_unseeded(repo: Path) -> None:
    """A profile referencing a missing tracked src aborts at
    validate_srcs_exist — the store must stay unseeded."""
    config = _write_config(repo, src="does-not-exist.md")

    result = _invoke(config)
    assert result.exit_code != 0, result.output
    assert not _seeded_in_store(), "a missing-src abort must not seed the store"
