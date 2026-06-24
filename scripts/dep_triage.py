#!/usr/bin/env python3
"""Deterministic dependency-triage check (ADVISORY, never mutates, never blocks).

A STANDALONE script (NOT a pytest test — pytest is skippable via markers and
``addopts``, which would silently disarm the contract; same reasoning as
:mod:`scripts.check_policy_lints` / :mod:`scripts.check_schema_gates`). It
wraps ``pip-audit --format json`` over the locked Python environment and turns
its findings into proposals for the self-improvement loop
(:mod:`scripts.proposals`):

- A **HIGH+** severity vulnerability (CVSS v3 base score ≥ 7.0) → a ``CVE``
  proposal. When pip-audit reports a ``fix_versions`` entry, the recommended
  upgrade is recorded in ``proposed_diff`` (a plain note, not a unified diff —
  the human edits the lockfile). Below the HIGH floor → no proposal (advisory
  fatigue control).
- A **major-version bump** available for a dependency (opt-in, ``--check-majors``)
  → a ``dep-major`` advisory proposal with an empty ``proposed_diff``.

Python-only: it audits the Python environment via pip-audit and makes no
attempt at other ecosystems.

INFRA-vs-violation discipline (the central correctness rule here): a tool
crash, a missing ``pip-audit`` binary, an unparseable / malformed JSON report,
or a pip-audit version older than the minimum that emits the relied-upon JSON
shape → exit ``2`` ("COULD NOT RUN"), printed to stderr — NEVER a false
"clean" (exit 0) and NEVER exit 1. Exit ``1`` is reserved for a real,
distinguishable HIGH+ finding. pip-audit's own exit 1 ("found vulns") is
expected and is NOT treated as an infra error — the JSON is still valid.

This mirrors the gitleaks-absent warn-and-continue convention in
:mod:`setforge.secrets` for the *style* of the warning, but the *contract*
differs: this is a CI triage gate, so a missing tool is a hard "could not run"
(exit 2), not a silent skip — a silent skip would let a CVE land unseen.

All subprocess invocations pass an argv list (never ``shell=True`` — SAFE-1).

Exit ``0`` clean / ``1`` HIGH+ finding / ``2`` INFRA error.

Invocation::

    uv run python scripts/dep_triage.py [--check-majors]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from scripts.proposals import Confidence, Proposal, emit

# Minimum pip-audit that emits the dependency/vulns JSON shape this check reads.
_MIN_PIP_AUDIT = (2, 4, 0)
# CVSS v3 base score at or above which a vulnerability is "HIGH+".
_HIGH_FLOOR = 7.0
_AUDIT_TIMEOUT_SECONDS = 300
_VERSION_RE = re.compile(r"pip-audit\s+(\d+)\.(\d+)\.(\d+)")
# A version token in `pip-audit index versions <pkg>` output, e.g. "(3.0.0)".
_PKG_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)(?:\.(\d+))?\b")


class InfraError(Exception):
    """The audit could not be run/trusted (absent tool, crash, malformed JSON,
    stale version) — exit 2, never a false clean."""


@dataclass(frozen=True, slots=True)
class CveFinding:
    """One HIGH+ vulnerability: the package, its installed version, the CVE/
    advisory id, the highest CVSS score seen, and a fix version if offered."""

    package: str
    version: str
    advisory_id: str
    score: float
    fix_version: str | None


def _resolve_pip_audit() -> str:
    path = shutil.which("pip-audit")
    if path is None:
        raise InfraError(
            "pip-audit not found on PATH — install it (uv add --dev pip-audit) "
            "so dependency triage can run"
        )
    return path


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an argv list (SAFE-1: never shell=True). A failure to even exec the
    tool (OSError / timeout) is an infra error, not a finding."""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,  # exit codes are interpreted by the caller
            timeout=_AUDIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise InfraError(
            f"{argv[0]} timed out after {_AUDIT_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise InfraError(f"could not execute {argv[0]}: {exc}") from exc


def _assert_minimum_version(pip_audit: str) -> None:
    result = _run([pip_audit, "--version"])
    m = _VERSION_RE.search(result.stdout) or _VERSION_RE.search(result.stderr)
    if m is None:
        raise InfraError(f"could not parse pip-audit version from: {result.stdout!r}")
    version = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if version < _MIN_PIP_AUDIT:
        need = ".".join(str(n) for n in _MIN_PIP_AUDIT)
        have = ".".join(str(n) for n in version)
        raise InfraError(
            f"pip-audit {have} is older than the required {need} (its JSON shape "
            "predates the fields this check reads)"
        )


def _audit_report(pip_audit: str) -> dict[str, object]:
    """Run the audit and return the parsed JSON. pip-audit exit 1 ("found vulns")
    is fine; only a crash (exit ≥ 2) or unparseable output is an infra error."""
    result = _run([pip_audit, "--format", "json", "--progress-spinner=off"])
    if result.returncode not in (0, 1):
        raise InfraError(
            f"pip-audit crashed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        report = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise InfraError(f"pip-audit JSON could not be parsed: {exc}") from exc
    if not isinstance(report, dict):
        raise InfraError(f"pip-audit JSON was not an object: {type(report).__name__}")
    return report


def _highest_cvss(vuln: dict[str, object]) -> float:
    """Highest CVSS v3 base score across a vuln's ``severity`` entries (0.0 when
    none present — pip-audit/OSV does not always carry a score)."""
    best = 0.0
    severities = vuln.get("severity")
    if not isinstance(severities, list):
        return best
    for sev in severities:
        if not isinstance(sev, dict):
            continue
        raw = sev.get("score")
        try:
            score = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        best = max(best, score)
    return best


def _cve_findings(report: dict[str, object]) -> list[CveFinding]:
    """Extract HIGH+ vulnerabilities. Uses ``.get()`` throughout — pip-audit
    output is external data and a missing key must not raise."""
    findings: list[CveFinding] = []
    deps = report.get("dependencies")
    if not isinstance(deps, list):
        return findings
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name", ""))
        version = str(dep.get("version", ""))
        vulns = dep.get("vulns")
        if not isinstance(vulns, list):
            continue
        for vuln in vulns:
            if not isinstance(vuln, dict):
                continue
            score = _highest_cvss(vuln)
            if score < _HIGH_FLOOR:
                continue
            fixes = vuln.get("fix_versions")
            fix_version = str(fixes[0]) if isinstance(fixes, list) and fixes else None
            findings.append(
                CveFinding(
                    package=name,
                    version=version,
                    advisory_id=str(vuln.get("id", "<unknown>")),
                    score=score,
                    fix_version=fix_version,
                )
            )
    return findings


def _emit_cve(finding: CveFinding) -> None:
    fix = (
        f"upgrade {finding.package} {finding.version} -> {finding.fix_version}"
        if finding.fix_version
        else ""
    )
    evidence = (
        f"pip-audit: {finding.advisory_id} affects {finding.package} "
        f"{finding.version} (CVSS {finding.score}); "
        + (f"fix in {finding.fix_version}" if finding.fix_version else "no fix yet")
    )
    emit(
        Proposal(
            source="dep-triage",
            category="CVE",
            evidence=evidence,
            proposed_diff=fix,
            confidence=Confidence.HIGH,
            file="pyproject.toml",
        )
    )


def _installed_packages(report: dict[str, object]) -> list[tuple[str, tuple[int, ...]]]:
    out: list[tuple[str, tuple[int, ...]]] = []
    deps = report.get("dependencies")
    if not isinstance(deps, list):
        return out
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name", ""))
        m = _PKG_VERSION_RE.search(str(dep.get("version", "")))
        if name and m:
            out.append((name, (int(m.group(1)),)))
    return out


def _latest_major(pip_audit: str, package: str) -> int | None:
    """Best-effort latest major from ``pip-audit index versions <pkg>`` (a
    pip-audit-bundled probe). Returns None when it cannot be determined — a
    probe miss is advisory-only and must never escalate to an infra error."""
    result = _run([pip_audit, "index", "versions", package])
    if result.returncode != 0:
        return None
    best: int | None = None
    for m in _PKG_VERSION_RE.finditer(result.stdout):
        major = int(m.group(1))
        best = major if best is None else max(best, major)
    return best


def _emit_major_bumps(pip_audit: str, report: dict[str, object]) -> None:
    for name, installed in _installed_packages(report):
        latest_major = _latest_major(pip_audit, name)
        if latest_major is None or latest_major <= installed[0]:
            continue
        emit(
            Proposal(
                source="dep-triage",
                category="dep-major",
                evidence=(
                    f"pip-audit index: {name} has a major release "
                    f"{latest_major}.x available (installed major {installed[0]})"
                ),
                proposed_diff="",
                confidence=Confidence.LOW,
                file="pyproject.toml",
            )
        )


def run(*, check_majors: bool = False) -> int:
    """Run the triage. Returns ``0`` clean / ``1`` HIGH+ CVE / ``2`` infra error."""
    try:
        pip_audit = _resolve_pip_audit()
        _assert_minimum_version(pip_audit)
        report = _audit_report(pip_audit)
    except InfraError as exc:
        print(f"dep-triage: COULD NOT RUN — {exc}", file=sys.stderr)
        return 2

    findings = _cve_findings(report)
    for finding in findings:
        print(
            f"dep-triage: {finding.advisory_id} {finding.package} "
            f"{finding.version} (CVSS {finding.score})",
            file=sys.stderr,
        )
        _emit_cve(finding)

    if check_majors:
        try:
            _emit_major_bumps(pip_audit, report)
        except InfraError as exc:
            # Major-bump probing is advisory garnish; never fail the whole run on it.
            print(f"dep-triage: major-bump probe skipped — {exc}", file=sys.stderr)

    return 1 if findings else 0


def main(argv: list[str] | None = None, *, check_majors: bool | None = None) -> int:
    # When invoked programmatically (tests, the orchestrator) default to an
    # EMPTY argv — never fall through to ``sys.argv``, which would parse the
    # caller's (e.g. pytest's) command line. Only ``__main__`` passes real argv.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-majors",
        action="store_true",
        help="also emit advisory proposals for available major-version bumps",
    )
    args = parser.parse_args([] if argv is None else argv)
    majors = args.check_majors if check_majors is None else check_majors
    return run(check_majors=majors)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
