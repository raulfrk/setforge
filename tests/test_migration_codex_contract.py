from pathlib import Path

import pytest

from setforge.config import GitHubReleasePackage, load_config
from setforge.errors import ConfigError
from setforge.migrations import MigrationRoots
from setforge.migrations._codex_contract import CodexContractMigration


def _roots(tmp_path: Path, body: str) -> MigrationRoots:
    path = tmp_path / "setforge.yaml"
    path.write_text(body)
    return MigrationRoots(cfg_path=path, repo_root=tmp_path, home=tmp_path / "home")


def test_codex_contract_forward_and_safe_reverse(tmp_path: Path) -> None:
    roots = _roots(
        tmp_path,
        "version: 1\nschema_version: '6.3'\nminimum_version: '6.4'\n"
        "tracked_files: {}\nprofiles: {}\n",
    )
    migration = CodexContractMigration()

    migration.apply(roots=roots)
    assert "schema_version: '6.4'" in roots.cfg_path.read_text()

    migration.reverse.apply(roots=roots)
    text = roots.cfg_path.read_text()
    assert "schema_version: '6.3'" in text
    assert "minimum_version: '6.3'" in text


def test_codex_contract_reverse_retains_valid_6_3_feature_floor(
    tmp_path: Path,
) -> None:
    roots = _roots(
        tmp_path,
        """version: 1
schema_version: '6.4'
minimum_version: '6.4'
tracked_files: {}
packages:
  tool:
    type: github_release
    repo: owner/repo
    binary: tool
    tag: v1.0.0
    install: ~/.local/bin/tool
    assets:
    - {asset: tool-linux, os: linux, arch: x86_64}
profiles: {}
""",
    )

    CodexContractMigration().reverse.apply(roots=roots)

    config = load_config(roots.cfg_path)
    assert config.schema_version == "6.3"
    assert config.minimum_version == "6.3"
    package = config.packages["tool"]
    assert isinstance(package, GitHubReleasePackage)
    assert package.type == "github_release"
    assert package.assets is not None
    assert [(asset.asset, asset.os, asset.arch) for asset in package.assets] == [
        ("tool-linux", "linux", "x86_64")
    ]


@pytest.mark.parametrize(
    "declaration",
    ["codex: {}\nprofiles: {}\n", "profiles: {default: {codex: {}}}\n"],
)
def test_codex_contract_reverse_refuses_declarations(
    tmp_path: Path, declaration: str
) -> None:
    roots = _roots(
        tmp_path,
        "version: 1\nschema_version: '6.4'\ntracked_files: {}\n" + declaration,
    )
    before = roots.cfg_path.read_bytes()

    with pytest.raises(ConfigError, match="while Codex declarations are present"):
        CodexContractMigration().reverse.apply(roots=roots)
    assert roots.cfg_path.read_bytes() == before
