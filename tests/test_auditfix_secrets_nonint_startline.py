"""Audit-fix regression tests: per-entry coercion on the exit-1 fail-closed path.

The secrets-scan gate runs ``_parse_gitleaks_json`` only when gitleaks exits 1
(secrets positively detected). A finding object carrying a non-numeric
``StartLine`` must NOT raise a bare ``ValueError`` (which the CLI top-level
``except SetforgeError`` handler would not catch — it would bubble as an
unhandled traceback with a non-clean exit). The fix coerces ``StartLine``
defensively to 0 so the finding is still produced and the install gate still
fires on the detected secret.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from setforge import binaries, secrets


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect binaries' LOCAL_CONFIG_PATH + clear CLI/env state per test."""
    monkeypatch.setattr(binaries, "LOCAL_CONFIG_PATH", tmp_path / "local.yaml")
    binaries._cli_overrides.clear()
    for name in binaries.SUPPORTED_BINARIES:
        monkeypatch.delenv(
            f"{binaries._ENV_VAR_PREFIX}{name.upper()}{binaries._ENV_VAR_SUFFIX}",
            raising=False,
        )


def _fake_run(
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a fake ``subprocess.run`` replacement returning a CompletedProcess."""

    def _runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _runner


def _scan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str):
    """Drive run_pre_deploy_scan with a faked exit-1 gitleaks emitting ``payload``."""
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )
    monkeypatch.setattr(
        secrets.subprocess, "run", _fake_run(returncode=1, stdout=payload)
    )
    return secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=tmp_path / "allow"
    )


def test_exit_one_nonint_startline_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-numeric StartLine must not raise a bare ValueError (fail-closed)."""
    payload = json.dumps(
        [
            {
                "RuleID": "github-pat",
                "File": "tracked/x.md",
                "StartLine": "not-a-number",
                "Secret": "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            }
        ]
    )
    # Old behavior raised ValueError here; the gate must instead produce a
    # finding (so the install still blocks) with a graceful line_number == 0.
    result = _scan(monkeypatch, tmp_path, payload)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rule_id == "github-pat"
    assert finding.line_number == 0
    assert finding.snippet.startswith("ghp_")


def test_exit_one_malformed_finding_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A structurally valid array with a hostile field type stays graceful."""
    payload = '[{"RuleID":"x","File":"a","StartLine":"oops","Secret":"s"}]'

    result = _scan(monkeypatch, tmp_path, payload)

    assert len(result.findings) == 1
    assert result.findings[0].line_number == 0


def test_exit_one_missing_startline_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A finding object lacking StartLine entirely yields line_number 0."""
    payload = json.dumps(
        [
            {
                "RuleID": "generic",
                "File": "tracked/y.md",
                "Secret": "sekret",
            }
        ]
    )

    result = _scan(monkeypatch, tmp_path, payload)

    assert len(result.findings) == 1
    assert result.findings[0].line_number == 0


def test_exit_one_null_startline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A JSON null StartLine (None) coerces to 0 via the `or 0` guard."""
    payload = json.dumps(
        [
            {
                "RuleID": "generic",
                "File": "tracked/z.md",
                "StartLine": None,
                "Secret": "sekret",
            }
        ]
    )

    result = _scan(monkeypatch, tmp_path, payload)

    assert len(result.findings) == 1
    assert result.findings[0].line_number == 0
