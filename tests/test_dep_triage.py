"""Unit tests for the dependency-triage check (scripts/dep_triage.py).

The check is ADVISORY: it never mutates files and never blocks. It wraps
``pip-audit --format json`` over the locked environment and turns its findings
into proposals:

- a HIGH+ severity vulnerability -> a ``CVE`` proposal (fix version, when
  pip-audit supplies one, lands in ``proposed_diff``);
- a vulnerability below the HIGH floor -> no proposal;
- a major-version bump available -> a ``dep-major`` advisory proposal
  (``proposed_diff`` empty).

INFRA-vs-violation discipline: ``pip-audit`` absent, a non-JSON / malformed
report, or a tool crash -> exit 2 ("COULD NOT RUN"), NEVER a false "clean" and
NEVER exit 1. Exit 1 is reserved for a real, distinguishable HIGH+ finding.

pip-audit is mocked via the pytest-subprocess ``fp`` fixture so no network or
real audit runs; the ledger is redirected to a tmp file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_subprocess import FakeProcess

from scripts import dep_triage


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    led = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("SETFORGE_PROPOSALS_LEDGER", str(led))
    return led


@pytest.fixture
def pip_audit_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``shutil.which`` is not intercepted by the fp fixture, so point the
    # resolver at a fake path; the fp fixture then mocks every invocation of it.
    monkeypatch.setattr(dep_triage.shutil, "which", lambda _name: "/fake/bin/pip-audit")


def _ledger_rows(led: Path) -> list[dict[str, str]]:
    if not led.exists():
        return []
    return [json.loads(ln) for ln in led.read_text().splitlines() if ln.strip()]


def _register_version(fp: FakeProcess, version: str = "2.7.3") -> None:
    fp.register([fp.any(), "--version"], stdout=f"pip-audit {version}\n")


HIGH_REPORT = {
    "dependencies": [
        {
            "name": "evilpkg",
            "version": "1.0.0",
            "vulns": [
                {
                    "id": "GHSA-xxxx-high",
                    "fix_versions": ["1.0.1"],
                    "severity": [{"type": "CVSS_V3", "score": "7.8"}],
                    "description": "remote code execution",
                }
            ],
        }
    ],
    "fixes": [],
}

LOW_REPORT = {
    "dependencies": [
        {
            "name": "mildpkg",
            "version": "2.0.0",
            "vulns": [
                {
                    "id": "GHSA-yyyy-low",
                    "fix_versions": ["2.0.1"],
                    "severity": [{"type": "CVSS_V3", "score": "3.1"}],
                    "description": "low-impact info leak",
                }
            ],
        }
    ],
    "fixes": [],
}


# --------------------------------------------------------------------------- #
# HIGH CVE -> proposal + exit 1
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pip_audit_on_path")
def test_high_cve_emits_proposal_and_exits_1(
    fp: FakeProcess, isolated_ledger: Path
) -> None:
    _register_version(fp)
    fp.register(
        [fp.any(), "--format", "json", fp.any()],
        stdout=json.dumps(HIGH_REPORT),
        returncode=1,  # pip-audit exits 1 when it finds vulns — NOT an infra error.
    )
    assert dep_triage.main() == 1
    cve_rows = [r for r in _ledger_rows(isolated_ledger) if r["category"] == "CVE"]
    assert cve_rows, "a CVE proposal must be emitted"
    assert "1.0.1" in cve_rows[0]["proposed_diff"]


# --------------------------------------------------------------------------- #
# Below the HIGH floor -> no proposal, exit 0
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pip_audit_on_path")
def test_low_severity_emits_nothing_and_exits_0(
    fp: FakeProcess, isolated_ledger: Path
) -> None:
    _register_version(fp)
    fp.register(
        [fp.any(), "--format", "json", fp.any()],
        stdout=json.dumps(LOW_REPORT),
        returncode=1,
    )
    assert dep_triage.main() == 0
    assert [r for r in _ledger_rows(isolated_ledger) if r["category"] == "CVE"] == []


# --------------------------------------------------------------------------- #
# CVSS floor boundary: 6.9 just below (no proposal), 7.0 at the floor (proposal)
# — brackets _HIGH_FLOOR so a moved floor or a flipped >= comparison is caught.
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pip_audit_on_path")
@pytest.mark.parametrize(
    ("score", "expected_exit", "expect_cve"),
    [("6.9", 0, False), ("7.0", 1, True)],
)
def test_cvss_floor_boundary(
    fp: FakeProcess,
    isolated_ledger: Path,
    score: str,
    expected_exit: int,
    expect_cve: bool,
) -> None:
    report = {
        "dependencies": [
            {
                "name": "edgepkg",
                "version": "1.0.0",
                "vulns": [
                    {
                        "id": "GHSA-edge",
                        "fix_versions": ["1.0.1"],
                        "severity": [{"type": "CVSS_V3", "score": score}],
                        "description": "boundary case",
                    }
                ],
            }
        ],
        "fixes": [],
    }
    _register_version(fp)
    fp.register(
        [fp.any(), "--format", "json", fp.any()],
        stdout=json.dumps(report),
        returncode=1,
    )
    assert dep_triage.main() == expected_exit
    cve_rows = [r for r in _ledger_rows(isolated_ledger) if r["category"] == "CVE"]
    assert bool(cve_rows) is expect_cve


# --------------------------------------------------------------------------- #
# Clean audit -> exit 0, no proposals
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pip_audit_on_path")
def test_clean_audit_exits_0(fp: FakeProcess, isolated_ledger: Path) -> None:
    _register_version(fp)
    fp.register(
        [fp.any(), "--format", "json", fp.any()],
        stdout=json.dumps({"dependencies": [], "fixes": []}),
        returncode=0,
    )
    assert dep_triage.main() == 0
    assert _ledger_rows(isolated_ledger) == []


# --------------------------------------------------------------------------- #
# pip-audit absent -> exit 2 (COULD NOT RUN), never false clean
# --------------------------------------------------------------------------- #
def test_pip_audit_absent_is_infra_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(dep_triage.shutil, "which", lambda _name: None)
    assert dep_triage.main() == 2
    assert "COULD NOT RUN" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Malformed JSON -> exit 2, never exit 1 / clean
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pip_audit_on_path")
def test_malformed_json_is_infra_exit_2(
    fp: FakeProcess, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_version(fp)
    fp.register(
        [fp.any(), "--format", "json", fp.any()],
        stdout="not json at all <<<",
        returncode=0,
    )
    assert dep_triage.main() == 2
    assert "COULD NOT RUN" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# pip-audit crash (exit code outside {0,1}) -> exit 2, never a verdict
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pip_audit_on_path")
def test_pip_audit_crash_is_infra_exit_2(
    fp: FakeProcess, capsys: pytest.CaptureFixture[str]
) -> None:
    # An exit code other than 0 (clean) or 1 (found vulns) is a tool crash, not
    # a finding — it must be COULD NOT RUN, never a false clean or a false BLOCK.
    _register_version(fp)
    fp.register(
        [fp.any(), "--format", "json", fp.any()],
        stdout="",
        stderr="Traceback (most recent call last): ...",
        returncode=2,
    )
    assert dep_triage.main() == 2
    assert "COULD NOT RUN" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Stale pip-audit version -> exit 2 (the JSON shape predates the contract)
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pip_audit_on_path")
def test_below_minimum_pip_audit_version_is_infra_exit_2(
    fp: FakeProcess, capsys: pytest.CaptureFixture[str]
) -> None:
    _register_version(fp, version="1.0.0")
    assert dep_triage.main() == 2
    assert "COULD NOT RUN" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Major-version bump available -> dep-major advisory proposal (empty diff)
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pip_audit_on_path")
def test_major_bump_emits_dep_major_advisory(
    fp: FakeProcess, isolated_ledger: Path
) -> None:
    _register_version(fp)
    report = {
        "dependencies": [
            {"name": "oldpkg", "version": "1.2.3", "vulns": []},
        ],
        "fixes": [],
    }
    fp.register(
        [fp.any(), "--format", "json", fp.any()],
        stdout=json.dumps(report),
        returncode=0,
    )
    # The latest-version probe is a second pip-audit-bundled call we inject.
    fp.register(
        [fp.any(), "index", "versions", "oldpkg"],
        stdout="oldpkg (3.0.0)\nAvailable versions: 3.0.0, 2.1.0, 1.2.3\n",
        returncode=0,
    )
    assert dep_triage.main(check_majors=True) == 0  # advisory only, never gates
    major = [r for r in _ledger_rows(isolated_ledger) if r["category"] == "dep-major"]
    assert major
    assert major[0]["proposed_diff"] == ""
