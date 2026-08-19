"""Closed-world checks for the schema-6 onboarding and command reference."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from setforge.cli import app
from setforge.config import Config, load_config

ROOT = Path(__file__).parents[1]
DOC_PATHS = (
    ROOT / "README.md",
    ROOT / "docs" / "configuration.md",
    ROOT / "docs" / "tutorial.md",
    ROOT / "docs" / "commands.md",
)
EXAMPLE_NAMES = frozenset(
    {
        "readme-minimal-schema6",
        "configuration-full-schema6",
        "tutorial-schema6",
    }
)
TOP_LEVEL_COMMANDS = frozenset(
    {
        "install",
        "compare",
        "cleanup-orphans",
        "cleanup",
        "capture",
        "sync",
        "revert",
        "recover",
        "validate",
        "fetch",
        "lock",
        "init",
        "upgrade",
        "migrate",
        "status",
        "stage",
        "inspect",
        "transitions",
        "ext",
        "plugin",
        "marketplace",
        "profile",
        "snapshot",
        "completion",
        "config",
    }
)
DOCUMENTED_FLAGS = {
    "lock": frozenset({"--profile", "--update", "--config"}),
    "install": frozenset({"--locked", "--no-fetch"}),
    "cleanup-orphans": frozenset(
        {"--profile", "--config", "--apply", "--yes", "--ignore", "--scan"}
    ),
}
RETIRED_PROFILE_FIELDS = frozenset(
    {"extensions", "claude_plugins", "cargo_binaries", "plugins_reconcile"}
)
FULL_EXAMPLE_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "minimum_version",
        "tracked_files",
        "marketplaces",
        "claude_plugins",
        "mcp_servers",
        "section_templates",
        "packages",
        "bundles",
        "profiles",
    }
)
FULL_EXAMPLE_PROFILE = frozenset(
    {
        "tracked_files",
        "packages",
        "bundles",
        "mcp_servers",
        "reconcile",
        "section_slots",
    }
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_EXAMPLE_RE = re.compile(
    r"<!-- setforge-doc-example: (?P<name>[a-z0-9-]+) -->\s*"
    r"```yaml\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def _docs_text() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in DOC_PATHS}


def _marked_region(text: str, start: str, end: str) -> str:
    _, separator, rest = text.partition(start)
    assert separator, f"missing marker {start!r}"
    region, separator, _ = rest.partition(end)
    assert separator, f"missing marker {end!r}"
    return region


def _table_first_column(region: str) -> frozenset[str]:
    return frozenset(
        match.group(1)
        for line in region.splitlines()
        if (match := re.match(r"^\| `([^`]+)` \|", line)) is not None
    )


def _help(*args: str) -> str:
    result = CliRunner().invoke(app, [*args, "--help"], color=False)
    assert result.exit_code == 0, result.stdout
    return _ANSI_RE.sub("", result.stdout)


def _help_option_tokens(*args: str) -> frozenset[str]:
    """Return long options from rendered Click option rows, not prose."""
    return frozenset(
        token
        for line in _help(*args).splitlines()
        if line.lstrip().startswith("-")
        for token in re.findall(r"--[a-z][a-z0-9-]*", line)
    )


def test_named_onboarding_yaml_examples_are_exact_schema6_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every named example passes the real strict config/reference validator."""
    examples: dict[str, str] = {}
    for path, text in _docs_text().items():
        for match in _EXAMPLE_RE.finditer(text):
            name = match.group("name")
            assert name not in examples, f"duplicate docs example {name!r} in {path}"
            examples[name] = match.group("body")

    assert examples.keys() == EXAMPLE_NAMES
    yaml = YAML(typ="safe")
    validate_cli = importlib.import_module("setforge.cli.validate")
    monkeypatch.setattr(validate_cli, "_LOCAL_CONFIG_PATH", tmp_path / "local.yaml")
    for name, body in examples.items():
        raw = yaml.load(body)
        repo = tmp_path / name
        config_path = repo / "setforge.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(body, encoding="utf-8")
        for tracked in raw["tracked_files"].values():
            source = repo / "tracked" / tracked["src"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"fixture for {name}\n", encoding="utf-8")
        for template in raw.get("section_templates", {}).values():
            source = repo / "templates" / template["src"]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"template for {name}\n", encoding="utf-8")

        config = load_config(config_path, tolerate_unknown=False)
        assert config.schema_version == "6.0", name
        validation = CliRunner().invoke(
            app, ["validate", "--all", "--config", str(config_path)]
        )
        assert validation.exit_code == 0, validation.stdout
        assert validation.stdout.strip() == "ok"
        for profile in raw["profiles"].values():
            assert RETIRED_PROFILE_FIELDS.isdisjoint(profile), name

    full_raw = yaml.load(examples["configuration-full-schema6"])
    assert full_raw.keys() == FULL_EXAMPLE_TOP_LEVEL
    assert full_raw["profiles"]["default"].keys() == FULL_EXAMPLE_PROFILE
    full = Config.model_validate(full_raw)
    assert full.packages
    assert full.bundles
    assert full.marketplaces
    assert full.claude_plugins
    assert full.mcp_servers
    assert full.section_templates
    profile = full.profiles["default"]
    assert profile.packages
    assert profile.bundles
    assert profile.mcp_servers
    assert profile.section_slots
    assert profile.reconcile.extensions.exclude


def test_current_docs_have_no_stale_schema5_onboarding_or_profile_examples() -> None:
    """Only the one explicitly historical schema-5 migration sentence survives."""
    texts = _docs_text()
    schema5_lines = [
        line.strip()
        for text in texts.values()
        for line in text.splitlines()
        if "5.0" in line
    ]
    assert schema5_lines == [
        "not schema-6 authoring syntax. The historical `5.0 -> 6.0` migration folds"
    ]

    profile_blocks = re.findall(
        r"(?ms)^profiles:\n(?P<body>(?:  .*\n?)+)", "\n".join(texts.values())
    )
    assert profile_blocks
    for block in profile_blocks:
        for field in RETIRED_PROFILE_FIELDS:
            assert re.search(rf"^    {re.escape(field)}:", block, re.MULTILINE) is None


def test_documented_top_level_command_inventory_matches_root_help() -> None:
    """The docs table, expected contract, and rendered root help are identical."""
    text = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    region = _marked_region(
        text,
        "<!-- setforge-doc-command-inventory:start -->",
        "<!-- setforge-doc-command-inventory:end -->",
    )
    documented = _table_first_column(region)
    rendered = frozenset(
        match.group(1)
        for line in _help().splitlines()
        if (match := re.match(r"^  ([a-z][a-z-]*)\s{2,}", line)) is not None
    )
    assert documented == TOP_LEVEL_COMMANDS
    assert rendered == TOP_LEVEL_COMMANDS


@pytest.mark.parametrize("command", tuple(DOCUMENTED_FLAGS))
def test_documented_reference_flags_exist_in_command_help(command: str) -> None:
    """Each closed flag table is complete for its claimed subset and still real."""
    text = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    marker = f"<!-- setforge-doc-flags: {command} -->"
    _, separator, tail = text.partition(marker)
    assert separator, f"missing flag-table marker for {command}"
    table = tail.split("\n\n", 1)[0]
    documented = _table_first_column(table)
    assert documented == DOCUMENTED_FLAGS[command]
    rendered_options = _help_option_tokens(command)
    assert documented <= rendered_options
