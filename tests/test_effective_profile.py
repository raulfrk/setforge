"""Unit contracts for the one shared + host-local profile resolver."""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

import setforge
from setforge import reconcile_adapter
from setforge.cli import app
from setforge.config import load_config, resolve_effective_profile
from setforge.overlay_provenance import OverlayOrigin

_PROFILE_CONSUMERS: tuple[tuple[str, str], ...] = (
    ("compare.py", "compare"),
    ("install.py", "_load_install_context"),
    ("sync.py", "_load_capture_preview"),
    ("inspect.py", "inspect"),
    ("status.py", "status"),
    ("snapshot.py", "_build_profile_ctx"),
    ("stage.py", "stage"),
    ("lock.py", "lock"),
    ("cleanup.py", "_resolve_declared"),
    ("orphans.py", "_detect_orphans_live"),
    ("orphans.py", "_detect_scan_live"),
    ("ext.py", "ext_list"),
    ("ext.py", "ext_reconcile"),
    ("ext.py", "_run_ext_reconcile"),
    ("plugins.py", "plugin_list"),
    ("plugins.py", "plugin_reconcile"),
    ("plugins.py", "_run_plugin_reconcile"),
    ("plugins.py", "sync_cache"),
    ("profile.py", "_run_profile_show"),
    ("revert.py", "_revert_symlink_deployments"),
    ("revert.py", "_revert_symlink_paths"),
)

_LEGACY_RESOLVER_ALLOWLIST: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("capture.py", "capture_profile", "resolve_profile"),
        ("compare.py", "compare_profile", "resolve_and_expand"),
        ("config.py", "resolve_and_expand", "resolve_profile"),
        ("config.py", "resolve_effective_profile", "resolve_and_expand"),
        ("vscode_extensions.py", "capture_extensions", "resolve_profile"),
        ("cli/profile.py", "_chain_resolved_by_name_field", "resolve_profile"),
        ("cli/profile.py", "_extensions_chain_by_name", "resolve_profile"),
        ("cli/profile.py", "_plugins_chain_by_name", "resolve_profile"),
        ("cli/validate.py", "_check_orphan_overlays", "resolve_and_expand"),
        ("cli/validate.py", "_check_profile_resolution", "resolve_and_expand"),
        (
            "migrations/_disposition_retire.py",
            "_build_legacy_records",
            "resolve_profile",
        ),
        ("migrations/_marker_retire.py", "_file_plans", "resolve_profile"),
        (
            "migrations/_span_surface_retire.py",
            "_build_section_folds",
            "resolve_profile",
        ),
    }
)

_IDENTIFIER = st.from_regex(r"[a-z][a-z0-9]{0,7}", fullmatch=True)


@pytest.mark.parametrize(("module_name", "function_name"), _PROFILE_CONSUMERS)
def test_profile_consumers_use_the_effective_resolver(
    module_name: str, function_name: str
) -> None:
    """Finite command inventory cannot drift back to shared-only resolution."""
    module_path = Path(setforge.__file__).parent / "cli" / module_name
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    calls = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "resolve_effective_profile" in calls, (
        f"{module_name}:{function_name} bypasses the effective-profile boundary"
    )
    assert not ({"resolve_profile", "resolve_and_expand"} & calls)


def _direct_calls(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Return direct call names, excluding nested function/class bodies."""
    calls: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            del node

        def visit_Call(self, node: ast.Call) -> None:
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if isinstance(name, str):
                calls.add(name)
            self.generic_visit(node)

    _Visitor().visit(function)
    return calls


def test_effective_consumer_inventory_matches_cli_call_sites() -> None:
    """The reviewed finite inventory is closed over every direct CLI caller."""
    cli_root = Path(setforge.__file__).parent / "cli"
    discovered: set[tuple[str, str]] = set()
    for module_path in cli_root.glob("*.py"):
        tree = ast.parse(
            module_path.read_text(encoding="utf-8"), filename=str(module_path)
        )
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                "resolve_effective_profile" in _direct_calls(node)
            ):
                discovered.add((module_path.name, node.name))
    assert discovered == set(_PROFILE_CONSUMERS)


def test_capture_preview_helper_has_exact_command_callers() -> None:
    """Both capture commands directly use the reviewed effective-profile seam."""
    module_path = Path(setforge.__file__).parent / "cli" / "sync.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    discovered = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "_load_capture_preview" in _direct_calls(node)
    }
    assert discovered == {"capture", "sync"}


def test_legacy_profile_resolution_calls_are_explicitly_allowlisted() -> None:
    """A new shared-only resolver call anywhere in production fails closed."""
    package_root = Path(setforge.__file__).parent
    discovered: set[tuple[str, str, str]] = set()
    for module_path in package_root.rglob("*.py"):
        tree = ast.parse(
            module_path.read_text(encoding="utf-8"), filename=str(module_path)
        )
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for resolver in _direct_calls(node) & {
                "resolve_profile",
                "resolve_and_expand",
            }:
                discovered.add(
                    (str(module_path.relative_to(package_root)), node.name, resolver)
                )
    assert discovered == _LEGACY_RESOLVER_ALLOWLIST


def test_effective_profile_applies_every_overlay_after_bundle_expansion(
    tmp_path: Path,
) -> None:
    """Synthetic bundle ids and all host-local axes resolve in one pass."""
    (tmp_path / "tracked").mkdir()
    (tmp_path / "tracked" / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(
        """\
version: 1
tracked_files: {}
bundles:
  tools:
    components:
      - id: launcher
        file:
          src: launch.sh
          dst: ~/.local/share/tools/launch.sh
          mode: 0o755
profiles:
  p:
    bundles: [tools]
""",
        encoding="utf-8",
    )
    local_path = tmp_path / "local.yaml"
    local_path.write_text(
        """\
tracked_files:
  tools.launcher:
    dst: /host/bin/launcher
marketplaces:
  add:
    local-tools:
      source: github
      repo: example/tools
plugins:
  add: [helper@local-tools]
extensions:
  add: [example.helper]
""",
        encoding="utf-8",
    )

    cfg = load_config(config_path)
    effective = resolve_effective_profile(
        cfg, "p", tmp_path, local_config_path=local_path
    )

    assert effective.resolved.tracked_files == ["tools.launcher"]
    assert cfg.tracked_files["tools.launcher"].dst == "/host/bin/launcher"
    assert effective.tracked_file_overrides["tools.launcher"].dst == Path(
        "/host/bin/launcher"
    )
    assert reconcile_adapter.plugin_bare_names(cfg, effective.resolved) == ["helper"]
    assert reconcile_adapter.extensions_input(cfg, effective.resolved).include == [
        "example.helper"
    ]
    assert "local-tools" in cfg.marketplaces
    assert any(
        item.value == "helper@local-tools" and item.origin is OverlayOrigin.LOCAL_ADD
        for item in effective.local_overlay.plugins
    )
    assert any(
        item.value == "example.helper" and item.origin is OverlayOrigin.LOCAL_ADD
        for item in effective.local_overlay.extensions
    )
    assert any(
        item.value == "local-tools" and item.origin is OverlayOrigin.LOCAL_ADD
        for item in effective.local_overlay.marketplaces
    )


@settings(max_examples=20)
@given(bundle_id=_IDENTIFIER, file_id=_IDENTIFIER, dst_leaf=_IDENTIFIER)
def test_bundle_component_override_order_is_stable(
    bundle_id: str, file_id: str, dst_leaf: str
) -> None:
    """Every valid synthetic id exists before its host override is applied."""
    with tempfile.TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        (root / "tracked").mkdir()
        (root / "tracked" / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        config_path = root / "setforge.yaml"
        config_path.write_text(
            f"""\
version: 1
tracked_files: {{}}
bundles:
  {bundle_id}:
    components:
      - id: {file_id}
        file:
          src: launch.sh
          dst: /shared/launch.sh
profiles:
  p:
    bundles: [{bundle_id}]
""",
            encoding="utf-8",
        )
        synthetic_id = f"{bundle_id}.{file_id}"
        effective_dst = f"/host/{dst_leaf}"
        local_path = root / "local.yaml"
        local_path.write_text(
            f"tracked_files:\n  {synthetic_id}:\n    dst: {effective_dst}\n",
            encoding="utf-8",
        )

        cfg = load_config(config_path)
        effective = resolve_effective_profile(
            cfg, "p", root, local_config_path=local_path
        )

        assert effective.resolved.tracked_files == [synthetic_id]
        assert cfg.tracked_files[synthetic_id].dst == effective_dst
        assert effective.tracked_file_overrides[synthetic_id].dst == Path(effective_dst)


def test_effective_profile_without_local_yaml_preserves_shared_resolution(
    tmp_path: Path,
) -> None:
    """Absent host state is a deterministic no-op, not a second profile shape."""
    (tmp_path / "tracked").mkdir()
    (tmp_path / "tracked" / "note.txt").write_text("hello\n", encoding="utf-8")
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(
        """\
version: 1
tracked_files:
  note:
    src: note.txt
    dst: ~/.config/example/note.txt
profiles:
  p:
    tracked_files: [note]
""",
        encoding="utf-8",
    )
    cfg = load_config(config_path)

    effective = resolve_effective_profile(
        cfg, "p", tmp_path, local_config_path=tmp_path / "absent.yaml"
    )

    assert effective.resolved.tracked_files == ["note"]
    assert cfg.tracked_files["note"].dst == "~/.config/example/note.txt"
    assert effective.tracked_file_overrides == {}
    assert effective.local_overlay.plugins == []
    assert effective.local_overlay.extensions == []
    assert effective.local_overlay.marketplaces == []


def test_validate_checks_effective_host_local_destination(tmp_path: Path) -> None:
    """Validate runs path checks against the merged destination, not shared-only."""
    (tmp_path / "tracked").mkdir()
    (tmp_path / "tracked" / "note.txt").write_text("hello\n", encoding="utf-8")
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(
        """\
version: 1
schema_version: "6.0"
tracked_files:
  note:
    src: note.txt
    dst: "{{ home }}/note.txt"
    template: true
profiles:
  p:
    tracked_files: [note]
""",
        encoding="utf-8",
    )
    (tmp_path / "local.yaml").write_text(
        'tracked_files:\n  note:\n    dst: "{{ missing_host_value }}/note.txt"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["validate", "--profile=p", f"--config={config_path}"]
    )

    assert result.exit_code == 1, result.output
    assert "missing_host_value" in result.output
    assert "undefined" in result.output.lower()
