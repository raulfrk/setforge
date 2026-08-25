from __future__ import annotations

import ast
import shlex
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from tests.docker.container_runtime import E2E_CONTAINER_LABEL

_ROOT = Path(__file__).resolve().parent.parent
_PR_ONLY = "github.event_name == 'pull_request'"
_DEFAULT_BRANCH_ONLY = (
    "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
)

_KNOWN_LIVE_UPSTREAM_TESTS = {
    "tests/docker/test_auditfix_plugin_add_marketplace.py": {
        "test_marketplace_add_binary_failure_does_not_leave_orphan_yaml_entry",
    },
    "tests/docker/test_e2e_docker.py": {
        "test_install_comprehensive_plugins_extensions",
        "test_install_verbose_emits_setforge_debug",
    },
    "tests/docker/test_e2e_docker_auditfix_ext_e2e.py": {
        "test_ext_add_live_marketplace_canary",
    },
    "tests/docker/test_e2e_docker_lock.py": {
        "test_install_locked_passes_on_match_and_fails_on_drift",
        "test_lock_writes_concrete_pins_across_ecosystems",
    },
    "tests/docker/test_e2e_docker_lock_strong_install.py": {
        "test_install_locked_extension_installs_verified_vsix_via_code",
    },
    "tests/docker/test_e2e_docker_toolchains.py": {
        "test_cargo_install_compiles_links_and_lands_on_path",
        "test_go_install_lands_on_path",
    },
}

_INFRA_SUPPORT_FILES = {
    "tests/docker/test_conftest_hash.py",
    "tests/docker/test_container_runtime.py",
    "tests/docker/test_image.py",
    "tests/docker/test_network.py",
    "tests/docker/test_offline_extension.py",
    "tests/test_e2e_xdist_prebuild.py",
    "tests/test_network_canary_contract.py",
}


def test_nightly_canary_lane_is_bounded_grouped_and_cleans_containers() -> None:
    workflow_data = _workflow("nightly.yml")
    workflow = (_ROOT / ".github/workflows/nightly.yml").read_text(encoding="utf-8")

    assert "needs" not in workflow_data["jobs"]["network-canaries"]
    assert "timeout-minutes: 60" in workflow
    assert workflow.count("timeout --kill-after=30s 20m uv run pytest") == 2
    assert workflow.count("--dist=loadgroup") == 2
    assert f"label={E2E_CONTAINER_LABEL}" in workflow
    assert "trap cleanup_canary_containers EXIT" in workflow


def _workflow(name: str) -> dict[str, Any]:
    data = YAML(typ="safe").load(
        (_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


def _deterministic_e2e_jobs(name: str, event: str) -> set[str]:
    """Select deterministic lanes for the workflow's declared event contract.

    The only job-level event guard supported here is the exact PR-only guard
    used by ``ci.yml``; adding another guard must extend these fixtures rather
    than silently pretending this is a general GitHub-expression evaluator.
    """
    workflow = _workflow(name)
    if event not in workflow["on"]:
        return set()
    selected: set[str] = set()
    for job_name, job in workflow["jobs"].items():
        rendered = repr(job)
        if "e2e_docker" not in rendered or "not network_canary" not in rendered:
            continue
        condition = str(job.get("if", ""))
        expected_condition = {
            ("ci.yml", "pr-e2e-smoke"): _PR_ONLY,
            ("nightly.yml", "e2e-docker-full"): _DEFAULT_BRANCH_ONLY,
        }.get((name, job_name))
        assert condition == expected_condition, (
            f"unexpected deterministic-lane condition on {job_name}: {condition!r}"
        )
        if condition == _PR_ONLY and event != "pull_request":
            continue
        selected.add(job_name)
    return selected


@pytest.mark.parametrize(
    ("workflow", "event", "expected"),
    [
        ("ci.yml", "pull_request", {"pr-e2e-smoke"}),
        ("ci.yml", "push", set()),
        ("nightly.yml", "schedule", {"e2e-docker-full"}),
        ("nightly.yml", "workflow_dispatch", {"e2e-docker-full"}),
    ],
)
def test_deterministic_e2e_lane_selection(
    workflow: str, event: str, expected: set[str]
) -> None:
    assert _deterministic_e2e_jobs(workflow, event) == expected


def test_deterministic_e2e_lanes_build_the_matching_image_target() -> None:
    ci_job = _workflow("ci.yml")["jobs"]["pr-e2e-smoke"]
    nightly_job = _workflow("nightly.yml")["jobs"]["e2e-docker-full"]
    ci_steps = repr(ci_job["steps"])
    nightly_steps = repr(nightly_job["steps"])
    ci_run = next(
        step["run"]
        for step in ci_job["steps"]
        if "uv run pytest" in step.get("run", "")
    )
    nightly_run = next(
        step["run"]
        for step in nightly_job["steps"]
        if "uv run pytest" in step.get("run", "")
    )

    assert '_image_tag("smoke")' in ci_steps
    assert any(
        step.get("with", {}).get("target") == "smoke" for step in ci_job["steps"]
    )
    assert "e2e_docker and smoke and not network_canary" in ci_run
    assert '_image_tag("full")' in nightly_steps
    assert any(
        step.get("with", {}).get("target") == "full" for step in nightly_job["steps"]
    )
    assert "e2e_docker and not network_canary" in nightly_run
    assert not any(
        token.startswith(("-n", "--numprocesses")) for token in shlex.split(nightly_run)
    )


def test_expensive_workflow_runs_do_not_overlap_superseded_runs() -> None:
    ci_jobs = _workflow("ci.yml")["jobs"]
    nightly = _workflow("nightly.yml")

    assert ci_jobs["pr-e2e-smoke"]["concurrency"] == {
        "group": "e2e-smoke-pr-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    }
    assert ci_jobs["pr-mutmut-diff"]["if"] == _PR_ONLY
    assert ci_jobs["pr-mutmut-diff"]["concurrency"] == {
        "group": "mutmut-pr-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    }
    assert nightly["concurrency"] == {
        "group": "nightly-${{ github.ref }}",
        "cancel-in-progress": True,
    }


def test_deterministic_lanes_exclude_network_canaries() -> None:
    ci = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    nightly = (_ROOT / ".github/workflows/nightly.yml").read_text(encoding="utf-8")

    assert "e2e_docker and smoke and not network_canary" in ci
    assert "target: full" not in ci
    assert 'e2e_docker and not network_canary"' not in ci
    assert "e2e_docker and not network_canary" in nightly


def _assert_ci_pytest_lane(command: str, *, marker: str, verdict: str) -> None:
    assert shlex.split(command) == [
        "uv",
        "run",
        "pytest",
        "-n",
        "${{ steps.pytest-workers.outputs.count }}",
        "--dist=worksteal",
        "-m",
        marker,
        verdict,
    ]


def test_regular_ci_splits_fast_coverage_from_slow_properties() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    worker_steps = [
        next(
            step
            for step in jobs[job]["steps"]
            if step.get("name") == "Select bounded pytest workers"
        )
        for job in ("tests", "slow-properties")
    ]
    coverage_step = next(
        step
        for step in jobs["tests"]["steps"]
        if step.get("name") == "Run unit and integration coverage in parallel"
    )
    slow_step = next(
        step
        for step in jobs["slow-properties"]["steps"]
        if step.get("name") == "Run exhaustive properties without coverage overhead"
    )

    assert coverage_step["env"] == {"HYPOTHESIS_PROFILE": "parallel"}
    assert slow_step["env"] == {"HYPOTHESIS_PROFILE": "parallel"}
    assert jobs["tests"]["timeout-minutes"] == 25
    assert jobs["slow-properties"]["timeout-minutes"] == 10
    worker_script = "\n".join(
        [
            "cpus=$(nproc)",
            "workers=$(( cpus < 6 ? cpus : 6 ))",
            'echo "count=$workers" >> "$GITHUB_OUTPUT"',
        ]
    )
    assert all(step["run"].strip() == worker_script for step in worker_steps)

    expected = [
        (
            coverage_step["run"],
            "not e2e_docker and not test_infra and not slow",
            "--cov-fail-under=85",
        ),
        (
            slow_step["run"],
            "slow and not e2e_docker and not test_infra",
            "--no-cov",
        ),
    ]
    for command, marker, verdict_option in expected:
        _assert_ci_pytest_lane(command, marker=marker, verdict=verdict_option)


def test_ci_infrastructure_lane_includes_every_test_infra_file() -> None:
    """A newly marked infrastructure file cannot silently miss every CI lane."""
    test_root = _ROOT / "tests"
    discovered = {
        path.relative_to(_ROOT).as_posix()
        for path in test_root.rglob("test_*.py")
        if any(
            isinstance(node, ast.Attribute)
            and ast.unparse(node) == "pytest.mark.test_infra"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }
    steps = _workflow("ci.yml")["jobs"]["tests"]["steps"]
    infra_run = next(
        step["run"]
        for step in steps
        if step.get("name") == "Test pytest/xdist infrastructure with branch coverage"
    )
    listed = {
        token
        for token in shlex.split(infra_run)
        if token.startswith("tests/") and token.endswith(".py")
    }

    assert discovered
    assert listed == discovered | _INFRA_SUPPORT_FILES


@pytest.mark.parametrize(
    "override",
    ["-n12", "-mslow", "--dist loadscope", "--no-cov", "--cov=setforge"],
)
def test_regular_ci_lane_contract_rejects_appended_overrides(override: str) -> None:
    command = (
        'uv run pytest -n "${{ steps.pytest-workers.outputs.count }}" '
        '--dist=worksteal -m "not e2e_docker and not test_infra and not slow" '
        f"--cov-fail-under=85 {override}"
    )

    with pytest.raises(AssertionError):
        _assert_ci_pytest_lane(
            command,
            marker="not e2e_docker and not test_infra and not slow",
            verdict="--cov-fail-under=85",
        )


def test_every_known_live_upstream_test_has_both_canary_gates() -> None:
    for relative_path, function_names in _KNOWN_LIVE_UPSTREAM_TESTS.items():
        tree = ast.parse((_ROOT / relative_path).read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name in function_names
        }
        assert functions.keys() == function_names
        for node in functions.values():
            decorators = {ast.unparse(decorator) for decorator in node.decorator_list}
            assert "pytest.mark.network_canary" in decorators
            assert "NETWORK_ONLY" in decorators
