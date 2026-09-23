from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_ROOT = Path(__file__).resolve().parent.parent
_RUNNERLESS_GUARD = "github.event_name == 'workbox_poll_only'"

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


def _workflow(name: str) -> dict[str, Any]:
    data = YAML(typ="safe").load(
        (_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


def test_ci_declares_runnerless_jobs_and_retains_hosted_secret_scan() -> None:
    workflow = _workflow("ci.yml")
    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["on"]["pull_request"]["branches"] == ["main"]
    jobs = workflow["jobs"]
    assert set(jobs) == {"workbox-unit", "workbox-integration", "secrets-scan"}
    for name in ("workbox-unit", "workbox-integration"):
        job = jobs[name]
        assert job["if"] == _RUNNERLESS_GUARD
        assert job["runs-on"] == "ubuntu-latest"
        assert len(job["steps"]) == 1
        assert set(job["steps"][0]) == {"run"}
        assert "home CI coordinator" in job["steps"][0]["run"]
    hosted = jobs["secrets-scan"]
    assert hosted["runs-on"] == "ubuntu-latest"
    assert any(
        step.get("uses") == "gitleaks/gitleaks-action@v2" for step in hosted["steps"]
    )


def test_nightly_declares_runnerless_full_suite_on_default_branch() -> None:
    workflow = _workflow("nightly.yml")
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["on"]["schedule"] == [{"cron": "0 4 * * *"}]
    assert workflow["concurrency"] == {
        "group": "nightly-${{ github.ref }}",
        "cancel-in-progress": True,
    }
    assert set(workflow["jobs"]) == {"workbox-full"}
    job = workflow["jobs"]["workbox-full"]
    assert job["if"] == _RUNNERLESS_GUARD
    assert job["runs-on"] == "ubuntu-latest"
    assert len(job["steps"]) == 1
    assert set(job["steps"][0]) == {"run"}
    assert "home CI coordinator" in job["steps"][0]["run"]


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
