from __future__ import annotations

import ast
from pathlib import Path

from tests.docker.container_runtime import E2E_CONTAINER_LABEL

_ROOT = Path(__file__).resolve().parent.parent

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


def test_nightly_canary_lane_is_bounded_grouped_and_cleans_containers() -> None:
    workflow = (_ROOT / ".github/workflows/nightly.yml").read_text(encoding="utf-8")

    assert "timeout-minutes: 60" in workflow
    assert workflow.count("timeout --kill-after=30s 20m uv run pytest") == 2
    assert workflow.count("--dist=loadgroup") == 2
    assert f"label={E2E_CONTAINER_LABEL}" in workflow
    assert "trap cleanup_canary_containers EXIT" in workflow


def test_deterministic_ci_lanes_exclude_network_canaries() -> None:
    ci = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    nightly = (_ROOT / ".github/workflows/nightly.yml").read_text(encoding="utf-8")

    assert "e2e_docker and smoke and not network_canary" in ci
    assert "e2e_docker and not network_canary" in ci
    assert "e2e_docker and not network_canary" in nightly


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
