#!/usr/bin/env python3
"""E2E verdict-manifest gate: keeps e2e_verdicts.toml in lock-step with the
real collected test set. STANDALONE (not pytest, which is skippable via
markers) so the contract can't be silently disarmed; fail-closed on a
nonzero/empty collect so a masked default ``-m`` exclude can't pass vacuously."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "tests" / "docker" / "e2e_verdicts.toml"

_NODE_ID_RE = re.compile(r"^\S+::")

VALID_VERDICTS: frozenset[str] = frozenset(
    {"keep", "change", "merge", "delete", "should-be-integration"}
)
PRUNE_VERDICTS: frozenset[str] = frozenset({"delete", "merge"})

REQUIRED_SMOKE_VERBS: frozenset[str] = frozenset(
    {
        "install",
        "sync",
        "compare",
        "revert",
        "validate",
        "init",
        "migrate",
        "upgrade",
        "secrets",
        "reconcile",
    }
)

# Raising any ceiling requires an explicit review of the new observable
# Docker boundary.  Lower these values whenever another right-sizing pass
# moves coverage down the pyramid.
ALL_E2E_EXPR = "e2e_docker"
DETERMINISTIC_E2E_EXPR = "e2e_docker and not network_canary"
PR_SMOKE_EXPR = "e2e_docker and smoke and not network_canary"

MAX_TOTAL_E2E_TESTS = 233
MAX_DETERMINISTIC_E2E_TESTS = 224
MAX_NETWORK_CANARY_TESTS = 9
MAX_PR_SMOKE_TESTS = 9


def _collect_node_ids(*, extra_marker_expr: str = "e2e_docker") -> list[str]:
    # Explicit -m overrides the default addopts exclude; a nonzero exit raises
    # (fail-closed) rather than returning an empty list.
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            "--collect-only",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-m",
            extra_marker_expr,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pytest --collect-only -m {extra_marker_expr!r} exited "
            f"{proc.returncode} (fail-closed):\n{proc.stdout}\n{proc.stderr}"
        )
    return [
        stripped
        for line in proc.stdout.splitlines()
        if _NODE_ID_RE.match(stripped := line.strip())
    ]


def _load_manifest() -> dict[str, dict[str, object]]:
    with MANIFEST_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    tests = data.get("tests")
    if not isinstance(tests, dict):
        raise RuntimeError(
            f"{MANIFEST_PATH} has no [tests] table — the manifest is empty or malformed"
        )
    return tests


def gate_collect_nonempty(collected: list[str]) -> list[str]:
    if len(collected) == 0:
        return [
            "fail-closed: pytest collected ZERO e2e_docker tests — the marker "
            "expression or default addopts exclude is masking the suite; refusing "
            "to pass vacuously"
        ]
    return []


def gate_suite_budgets(
    collected: list[str],
    deterministic_collected: list[str],
    network_collected: list[str],
    smoke_collected: list[str],
) -> list[str]:
    """Prevent gradual Docker-suite growth from erasing the speed win."""
    out: list[str] = []
    if len(collected) > MAX_TOTAL_E2E_TESTS:
        out.append(
            f"suite-budget: collected {len(collected)} Docker tests, budget is "
            f"{MAX_TOTAL_E2E_TESTS}; prove the new host/tool/TTY/process boundary "
            "or move the coverage to integration"
        )
    if len(deterministic_collected) > MAX_DETERMINISTIC_E2E_TESTS:
        out.append(
            f"suite-budget: collected {len(deterministic_collected)} deterministic "
            f"Docker tests, budget is {MAX_DETERMINISTIC_E2E_TESTS}; prove the new "
            "host/tool/TTY/process boundary or "
            "move the coverage to integration"
        )
    if len(network_collected) > MAX_NETWORK_CANARY_TESTS:
        out.append(
            f"suite-budget: collected {len(network_collected)} network canaries, "
            f"budget is {MAX_NETWORK_CANARY_TESTS}; keep upstream probes "
            "separately bounded"
        )
    if len(smoke_collected) > MAX_PR_SMOKE_TESTS:
        out.append(
            f"suite-budget: collected {len(smoke_collected)} smoke tests, budget "
            f"is {MAX_PR_SMOKE_TESTS}; keep the PR lane to one golden path per verb"
        )
    return out


def gate_row_shape(manifest: dict[str, dict[str, object]]) -> list[str]:
    out: list[str] = []
    for node_id, row in manifest.items():
        verdict = row.get("verdict")
        if verdict not in VALID_VERDICTS:
            out.append(
                f"row-shape: {node_id!r} has verdict {verdict!r}, "
                f"not one of {sorted(VALID_VERDICTS)}"
            )
        if not isinstance(row.get("smoke"), bool):
            out.append(f"row-shape: {node_id!r} 'smoke' must be a bool")
        if not isinstance(row.get("signal"), str) or not row.get("signal"):
            out.append(f"row-shape: {node_id!r} 'signal' must be a non-empty string")
        if not isinstance(row.get("verbs"), list) or not row.get("verbs"):
            out.append(f"row-shape: {node_id!r} 'verbs' must be a non-empty list")
        if verdict in PRUNE_VERDICTS:
            sup = row.get("superseded_by")
            if not isinstance(sup, list) or not sup:
                out.append(
                    f"row-shape: {node_id!r} verdict {verdict!r} requires a non-empty "
                    f"'superseded_by' list naming the sibling(s) that assert the "
                    f"same behavior"
                )
    return out


def gate_set_equality(
    collected: list[str], manifest: dict[str, dict[str, object]]
) -> list[str]:
    collected_set = set(collected)
    manifest_set = set(manifest)
    out: list[str] = []
    missing_rows = sorted(collected_set - manifest_set)
    for node_id in missing_rows:
        out.append(
            f"set-equality: collected id has NO manifest row (add a verdict): {node_id}"
        )
    stale_rows = sorted(manifest_set - collected_set)
    for node_id in stale_rows:
        out.append(
            f"set-equality: manifest row names a NON-collected id "
            f"(rename/remove it): {node_id}"
        )
    return out


def gate_uniqueness(collected: list[str]) -> list[str]:
    seen: set[str] = set()
    dups: set[str] = set()
    for node_id in collected:
        if node_id in seen:
            dups.add(node_id)
        seen.add(node_id)
    return [f"uniqueness: node id collected more than once: {d}" for d in sorted(dups)]


def gate_superseded_by_live(
    collected: list[str], manifest: dict[str, dict[str, object]]
) -> list[str]:
    # Guards mutual-deletion: a superseded_by target must itself still be collected.
    collected_set = set(collected)
    out: list[str] = []
    for node_id, row in manifest.items():
        if row.get("verdict") not in PRUNE_VERDICTS:
            continue
        superseded = row.get("superseded_by", [])
        if not isinstance(superseded, list):
            continue
        for sup in superseded:
            if sup not in collected_set:
                out.append(
                    f"superseded-by: {node_id!r} is marked "
                    f"{row.get('verdict')!r} but its superseded_by target "
                    f"{sup!r} is NOT in the collected set (mutual-deletion / stale "
                    f"reference)"
                )
    return out


def gate_verb_smoke_coverage(
    smoke_collected: list[str], manifest: dict[str, dict[str, object]]
) -> list[str]:
    smoke_set = set(smoke_collected)
    out: list[str] = []

    manifest_smoke_ids = {
        node_id for node_id, row in manifest.items() if row.get("smoke") is True
    }
    claimed_but_not_marked = sorted(manifest_smoke_ids - smoke_set)
    for node_id in claimed_but_not_marked:
        out.append(
            f"verb-smoke: manifest marks {node_id!r} smoke=true but it is NOT "
            f"collected under -m 'e2e_docker and smoke' (missing/typo'd "
            f"@pytest.mark.smoke?)"
        )
    marked_but_not_claimed = sorted(smoke_set - manifest_smoke_ids)
    for node_id in marked_but_not_claimed:
        out.append(
            f"verb-smoke: {node_id!r} carries @pytest.mark.smoke but the manifest "
            f"row is not smoke=true (out-of-sync)"
        )

    # Every required verb must be covered by at least one REAL smoke id.
    covered_verbs: set[str] = set()
    for node_id in smoke_set & manifest_smoke_ids:
        verbs = manifest[node_id].get("verbs", [])
        if isinstance(verbs, list):
            covered_verbs.update(str(v) for v in verbs)
    for verb in sorted(REQUIRED_SMOKE_VERBS - covered_verbs):
        out.append(
            f"verb-smoke: verb {verb!r} has NO smoke-collected golden-path test "
            f"(add @pytest.mark.smoke to one and set smoke=true on its row)"
        )
    return out


def run_all_gates() -> list[str]:
    try:
        collected = _collect_node_ids(extra_marker_expr=ALL_E2E_EXPR)
    except RuntimeError as err:
        return [str(err)]

    violations = gate_collect_nonempty(collected)
    if violations:
        return violations

    try:
        manifest = _load_manifest()
    except (OSError, RuntimeError, tomllib.TOMLDecodeError) as err:
        return [f"manifest-load: {err}"]

    violations.extend(gate_row_shape(manifest))
    violations.extend(gate_uniqueness(collected))
    violations.extend(gate_set_equality(collected, manifest))
    violations.extend(gate_superseded_by_live(collected, manifest))

    try:
        deterministic_collected = _collect_node_ids(
            extra_marker_expr=DETERMINISTIC_E2E_EXPR
        )
    except RuntimeError as err:
        return [*violations, f"deterministic-collect: {err}"]
    try:
        smoke_collected = _collect_node_ids(extra_marker_expr=PR_SMOKE_EXPR)
    except RuntimeError as err:
        return [*violations, f"smoke-collect: {err}"]
    network_collected = sorted(set(collected) - set(deterministic_collected))
    violations.extend(
        gate_suite_budgets(
            collected,
            deterministic_collected,
            network_collected,
            smoke_collected,
        )
    )
    violations.extend(gate_verb_smoke_coverage(smoke_collected, manifest))
    return violations


def main() -> int:
    violations = run_all_gates()
    if violations:
        print("E2E verdict-manifest gate FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print(
        "E2E verdict-manifest gate passed: set-equality, uniqueness, "
        "superseded-by-liveness, suite-budgets, verb-smoke-coverage, fail-closed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
