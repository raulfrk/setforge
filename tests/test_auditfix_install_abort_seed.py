"""Regression tests: an aborted install must not mutate local.yaml.

The section-template seed-commit and the legacy ``host_local_sections`` →
OVERLAY-span migration are in-place writes to ``local.yaml``. They were
once committed UNDER the profile lock but BEFORE the
validate-srcs / unexpected-drift / secrets-scan abort gates, so any of
those gates aborting left ``local.yaml`` silently seeded and/or migrated
with NO transition record to undo it (``setforge revert`` would then
reverse an unrelated prior transition or find nothing).

The fix relocates both writes to AFTER every refuse-before-write gate.
These tests pin that ordering: each drives an abort gate that fires
after the (former) seed point and asserts ``local.yaml`` is byte-identical
to its pre-install state — neither seeded nor span-migrated.

They mirror ``test_install_section_templates``'s
``test_install_welcome_decline_leaves_local_unseeded`` /
``test_install_git_check_abort_leaves_local_unseeded`` (which cover the
gates that already fired before the seed) for the gates that fire after it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from click.testing import Result
from typer.testing import CliRunner

from setforge import source as source_mod
from setforge.cli import app
from setforge.secrets import SecretFinding, SecretsScanResult

_PROFILE = "seed-test"

_DOC = """\
# Title

## Notes

upstream notes body
"""

_TEMPLATE_BODY = "SEEDED PYTHON CONVENTIONS\n"

_LEGACY_LOCAL_YAML = (
    "tracked_files:\n"
    "  doc:\n"
    "    host_local_sections:\n"
    "      preexisting:\n"
    "        anchor:\n"
    "          kind: after-heading\n"
    "          value: Notes\n"
    "        body: |\n"
    "          PRE-EXISTING HOST BODY\n"
)


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


def _local_yaml_path(tmp_path: Path) -> Path:
    # Matches conftest._isolated_local_config's LOCAL_CONFIG_PATH redirect.
    return tmp_path / "local.yaml"


def _is_seeded(local: Path) -> bool:
    """True when local.yaml carries the seeded template body."""
    if not local.exists():
        return False
    return "SEEDED PYTHON CONVENTIONS" in local.read_text(encoding="utf-8")


def _is_span_migrated(local: Path) -> bool:
    """True when a legacy host_local_sections block was retired into spans."""
    if not local.exists():
        return False
    text = local.read_text(encoding="utf-8")
    return "host_local_sections" not in text and "spans" in text


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


def test_secrets_abort_leaves_local_unseeded(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secrets-scan abort fires AFTER the (former) seed point — local.yaml
    must stay unwritten."""
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    assert not local.exists()

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
    assert not _is_seeded(local), "a secrets abort must not seed local.yaml: " + (
        local.read_text(encoding="utf-8") if local.exists() else "<absent>"
    )
    assert source_mod._pending_seed is None


def test_secrets_abort_leaves_local_yaml_byte_identical(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a pre-existing legacy host_local_sections block, a secrets abort
    must leave local.yaml byte-identical — neither seeded nor span-migrated."""
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    local.write_text(_LEGACY_LOCAL_YAML, encoding="utf-8")
    pre_bytes = local.read_bytes()

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
    assert local.read_bytes() == pre_bytes, (
        "a secrets abort must not mutate local.yaml; got:\n"
        + local.read_text(encoding="utf-8")
    )
    assert not _is_span_migrated(local)


def test_drift_gate_abort_leaves_local_unseeded(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unexpected-drift reject fires AFTER the (former) seed point —
    local.yaml must stay unwritten."""
    config = _write_config(repo)
    local = _local_yaml_path(tmp_path)
    local.write_text(_LEGACY_LOCAL_YAML, encoding="utf-8")
    pre_bytes = local.read_bytes()

    def _reject(**_kwargs: object) -> None:
        raise typer.Exit(code=2)

    monkeypatch.setattr("setforge.cli.install._run_predeploy_gates", _reject)

    result = _invoke(config)
    assert result.exit_code != 0
    assert local.read_bytes() == pre_bytes, (
        "a drift-gate abort must not mutate local.yaml; got:\n"
        + local.read_text(encoding="utf-8")
    )
    assert not _is_seeded(local)
    assert not _is_span_migrated(local)
    assert source_mod._pending_seed is None


def test_validate_srcs_abort_leaves_local_un_migrated(
    repo: Path, tmp_path: Path
) -> None:
    """A profile referencing a missing tracked src aborts at
    validate_srcs_exist — a pre-existing legacy block must stay un-migrated."""
    config = _write_config(repo, src="does-not-exist.md")
    local = _local_yaml_path(tmp_path)
    local.write_text(_LEGACY_LOCAL_YAML, encoding="utf-8")
    pre_bytes = local.read_bytes()

    result = _invoke(config)
    assert result.exit_code != 0, result.output
    assert local.read_bytes() == pre_bytes, (
        "a missing-src abort must not migrate local.yaml; got:\n"
        + local.read_text(encoding="utf-8")
    )
    assert not _is_span_migrated(local)
