#!/usr/bin/env python3
"""E2E suite gate: Docker-suite test-count ceilings per lane, plus a golden-path
smoke test for every required verb. STANDALONE (not pytest, which is skippable
via markers) so the contract can't be silently disarmed; fail-closed on a
nonzero/empty collect so a masked default ``-m`` exclude can't pass vacuously."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_NODE_ID_RE = re.compile(r"^\S+::")

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

# The verbs each @pytest.mark.smoke test is the golden path for (paths under
# tests/docker/).
_SMOKE_VERBS_BY_TEST: dict[str, tuple[str, ...]] = {
    "test_e2e_docker_codex_parity.py::test_mixed_codex_profile_converges_and_rolls_back_across_processes": (  # noqa: E501
        "validate",
        "install",
        "compare",
        "sync",
        "revert",
    ),
    "test_e2e_docker_auditfix_ext_e2e.py::test_ext_reconcile_live_applies_and_installs": (  # noqa: E501
        "reconcile",
    ),
    "test_e2e_docker_file_mode.py::test_mode_e2e_compare_flags_drift_after_manual_chmod": (  # noqa: E501
        "compare",
    ),
    "test_e2e_docker_meta_schema.py::test_transition_metadata_persists_across_cli_lifecycle": (  # noqa: E501
        "install",
        "sync",
        "revert",
    ),
    "test_e2e_docker_migrate.py::test_migrate_check_lists_the_stamp": ("migrate",),
    "test_e2e_docker.py::test_e2e_docker_init_fresh": ("init",),
    "test_e2e_docker.py::test_e2e_docker_install_secrets_scan_clean": ("secrets",),
    "test_e2e_docker.py::test_e2e_docker_upgrade_check_mode": ("upgrade",),
    "test_e2e_docker.py::test_install_minimal_floor": ("install",),
    "test_e2e_docker.py::test_validate_clean_yaml_exit_zero": ("validate",),
}

# Raising any ceiling requires an explicit review of the new observable
# Docker boundary.  Lower these values whenever another right-sizing pass
# moves coverage down the pyramid.
ALL_E2E_EXPR = "e2e_docker"
DETERMINISTIC_E2E_EXPR = "e2e_docker and not network_canary"
PR_SMOKE_EXPR = "e2e_docker and smoke and not network_canary"

MAX_TOTAL_E2E_TESTS = 181
MAX_DETERMINISTIC_E2E_TESTS = 172
MAX_NETWORK_CANARY_TESTS = 9
MAX_PR_SMOKE_TESTS = 10


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


SMOKE_TEST_VERBS = {
    f"tests/docker/{name}": verbs for name, verbs in _SMOKE_VERBS_BY_TEST.items()
}


def gate_verb_smoke_coverage(smoke_collected: list[str]) -> list[str]:
    covered: set[str] = set()
    for node_id in smoke_collected:
        covered.update(SMOKE_TEST_VERBS.get(node_id, ()))
    return [
        f"verb-smoke: verb {verb!r} has NO collected smoke golden-path test "
        f"(mark one @pytest.mark.smoke and list it in SMOKE_TEST_VERBS)"
        for verb in sorted(REQUIRED_SMOKE_VERBS - covered)
    ]


def run_all_gates() -> list[str]:
    try:
        collected = _collect_node_ids(extra_marker_expr=ALL_E2E_EXPR)
    except RuntimeError as err:
        return [str(err)]

    violations = gate_collect_nonempty(collected)
    if violations:
        return violations

    try:
        deterministic_collected = _collect_node_ids(
            extra_marker_expr=DETERMINISTIC_E2E_EXPR
        )
    except RuntimeError as err:
        return [f"deterministic-collect: {err}"]
    try:
        smoke_collected = _collect_node_ids(extra_marker_expr=PR_SMOKE_EXPR)
    except RuntimeError as err:
        return [f"smoke-collect: {err}"]
    network_collected = sorted(set(collected) - set(deterministic_collected))
    violations.extend(
        gate_suite_budgets(
            collected,
            deterministic_collected,
            network_collected,
            smoke_collected,
        )
    )
    violations.extend(gate_verb_smoke_coverage(smoke_collected))
    return violations


def main() -> int:
    violations = run_all_gates()
    if violations:
        print("E2E suite gate FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print("E2E suite gate passed: suite-budgets, verb-smoke-coverage, fail-closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
