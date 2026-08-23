"""Fast contracts for the Docker-suite test-count ceilings."""

import pytest

from scripts.check_e2e_manifest import (
    ALL_E2E_EXPR,
    DETERMINISTIC_E2E_EXPR,
    MAX_DETERMINISTIC_E2E_TESTS,
    MAX_NETWORK_CANARY_TESTS,
    MAX_PR_SMOKE_TESTS,
    MAX_TOTAL_E2E_TESTS,
    PR_SMOKE_EXPR,
    gate_suite_budgets,
    run_all_gates,
)


def test_suite_budgets_accept_current_ceilings() -> None:
    assert (
        gate_suite_budgets(
            [f"e2e-{i}" for i in range(MAX_TOTAL_E2E_TESTS)],
            [f"deterministic-{i}" for i in range(MAX_DETERMINISTIC_E2E_TESTS)],
            [f"network-{i}" for i in range(MAX_NETWORK_CANARY_TESTS)],
            [f"smoke-{i}" for i in range(MAX_PR_SMOKE_TESTS)],
        )
        == []
    )


def test_suite_budgets_report_each_exceeded_lane() -> None:
    violations = gate_suite_budgets(
        [f"e2e-{i}" for i in range(MAX_TOTAL_E2E_TESTS + 1)],
        [f"deterministic-{i}" for i in range(MAX_DETERMINISTIC_E2E_TESTS + 1)],
        [f"network-{i}" for i in range(MAX_NETWORK_CANARY_TESTS + 1)],
        [f"smoke-{i}" for i in range(MAX_PR_SMOKE_TESTS + 1)],
    )
    assert len(violations) == 4
    assert "235 Docker tests" in violations[0]
    assert "226 deterministic Docker tests" in violations[1]
    assert "10 network canaries" in violations[2]
    assert "11 smoke tests" in violations[3]


def test_run_all_gates_collects_exact_ci_lane_expressions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expressions: list[str] = []

    def _collect(*, extra_marker_expr: str) -> list[str]:
        expressions.append(extra_marker_expr)
        return [] if extra_marker_expr == PR_SMOKE_EXPR else ["test"]

    manifest = {
        "test": {
            "verdict": "keep",
            "smoke": False,
            "signal": "test signal",
            "verbs": ["install"],
        }
    }
    monkeypatch.setattr("scripts.check_e2e_manifest._collect_node_ids", _collect)
    monkeypatch.setattr("scripts.check_e2e_manifest._load_manifest", lambda: manifest)
    run_all_gates()
    assert expressions == [ALL_E2E_EXPR, DETERMINISTIC_E2E_EXPR, PR_SMOKE_EXPR]


@pytest.mark.parametrize(
    ("failed_expr", "label"),
    [
        (DETERMINISTIC_E2E_EXPR, "deterministic-collect:"),
        (PR_SMOKE_EXPR, "smoke-collect:"),
    ],
)
def test_run_all_gates_labels_lane_collection_failures(
    monkeypatch: pytest.MonkeyPatch,
    failed_expr: str,
    label: str,
) -> None:
    def _collect(*, extra_marker_expr: str) -> list[str]:
        if extra_marker_expr == failed_expr:
            raise RuntimeError("collection failed")
        return ["test"]

    manifest = {
        "test": {
            "verdict": "keep",
            "smoke": True,
            "signal": "test signal",
            "verbs": ["install"],
        }
    }
    monkeypatch.setattr("scripts.check_e2e_manifest._collect_node_ids", _collect)
    monkeypatch.setattr("scripts.check_e2e_manifest._load_manifest", lambda: manifest)
    violations = run_all_gates()
    assert any(violation.startswith(label) for violation in violations)
