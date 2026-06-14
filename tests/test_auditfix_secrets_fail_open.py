"""Regression: gitleaks exit-1 with an unparseable report must fail closed.

Audit finding ``secrets_fail_open``: when ``gitleaks detect`` exits 1 it has
positively detected ≥1 secret. If its stdout cannot be parsed as a JSON list
(a banner line interleaved into ``--report-path=/dev/stdout``, a version-format
change, or partial output), the old code returned an empty
:class:`SecretsScanResult` plus a yellow warning. The install gate
(``if scan_result.findings: ...``) reads empty findings as "clean" and deploys
the tracked tree even though gitleaks flagged secrets — a fail-open security
control. The fix raises :class:`SetforgeError` instead, blocking the install.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from setforge import binaries, secrets
from setforge.errors import SetforgeError


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
    """Return a fake ``subprocess.run`` returning a fixed CompletedProcess."""

    def _runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _runner


# Unparseable exit-1 stdout variants: an interleaved log/banner line ahead of a
# valid array, a truncated object, a non-list payload, and an empty report.
@pytest.mark.parametrize(
    "stdout",
    [
        "WARN gitleaks: leaks found\n[]",
        "{partial",
        '{"RuleID": "x"}',  # a dict, not a list
        "",  # exit 1 but nothing on stdout
        "   \n",  # whitespace-only
    ],
)
def test_exit_one_unparseable_report_raises_not_clean(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
) -> None:
    """Exit 1 + unparseable stdout must raise, never return a clean result."""
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )
    monkeypatch.setattr(
        secrets.subprocess, "run", _fake_run(returncode=1, stdout=stdout)
    )

    with pytest.raises(SetforgeError) as excinfo:
        secrets.run_pre_deploy_scan(
            tracked_root=tmp_path, allowlist_path=tmp_path / "allow"
        )

    # The message must name the detection (exit 1) and the refusal to deploy
    # so the install gate's downstream handler renders an actionable error.
    message = str(excinfo.value)
    assert "exit 1" in message
    assert "refusing to deploy" in message


def test_exit_one_valid_empty_list_is_not_a_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A well-formed empty ``[]`` parses fine (all findings allowlisted)."""
    monkeypatch.setattr(
        secrets.binaries, "resolve_binary", lambda _name: Path("/fake/gitleaks")
    )
    monkeypatch.setattr(secrets.subprocess, "run", _fake_run(returncode=1, stdout="[]"))

    # Must NOT raise: a valid array that happens to be empty is a successful
    # parse, distinct from the fail-open unparseable case above.
    result = secrets.run_pre_deploy_scan(
        tracked_root=tmp_path, allowlist_path=tmp_path / "allow"
    )

    assert result.findings == ()
