from pathlib import Path

import pytest

from setforge.errors import ConfigError
from setforge.migrations import MigrationRoots
from setforge.migrations._codex_mcp_scope import CodexMcpScopeMigration


def _roots(tmp_path: Path, body: str) -> MigrationRoots:
    path = tmp_path / "setforge.yaml"
    path.write_text(body)
    return MigrationRoots(cfg_path=path, repo_root=tmp_path, home=tmp_path / "home")


def test_codex_mcp_scope_forward_and_safe_reverse(tmp_path: Path) -> None:
    roots = _roots(
        tmp_path,
        "version: 1\nschema_version: '6.4'\nminimum_version: '6.5'\n"
        "tracked_files: {}\nprofiles: {}\n",
    )
    migration = CodexMcpScopeMigration()

    migration.apply(roots=roots)
    assert "schema_version: '6.5'" in roots.cfg_path.read_text()

    migration.reverse.apply(roots=roots)
    text = roots.cfg_path.read_text()
    assert "schema_version: '6.4'" in text
    assert "minimum_version: '6.4'" in text


@pytest.mark.parametrize("field", ["scope: project", "project: app"])
def test_codex_mcp_scope_reverse_refuses_new_fields(tmp_path: Path, field: str) -> None:
    roots = _roots(
        tmp_path,
        "version: 1\nschema_version: '6.5'\ntracked_files: {}\n"
        "codex:\n  mcp_servers:\n    api:\n      transport: http\n"
        "      url: https://mcp.example.test\n"
        f"      {field}\n"
        "profiles: {}\n",
    )
    before = roots.cfg_path.read_bytes()

    with pytest.raises(ConfigError, match="scoped Codex MCP"):
        CodexMcpScopeMigration().reverse.apply(roots=roots)
    assert roots.cfg_path.read_bytes() == before
