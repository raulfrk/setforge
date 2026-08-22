from pathlib import Path

import pytest
import typer

from setforge.capture import preview_capture_profile
from setforge.cli.stage import _refuse_generated_stage_target
from setforge.config import load_config, resolve_and_expand
from setforge.errors import ConfigError, InvariantViolation


def _write_config(repo: Path, body: str) -> Path:
    path = repo / "setforge.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_tree_intent_requires_schema_six_two_floor(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path,
        "schema_version: '6.1'\n"
        "minimum_version: '6.1'\n"
        "tracked_files:\n"
        "  tools: {src: tools, dst: /tmp/tools, tree: {}}\n"
        "profiles: {p: {tracked_files: [tools]}}\n",
    )
    with pytest.raises(ConfigError, match=r"managed trees require.*6.2"):
        load_config(config)


def test_tree_destination_overlap_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "tracked").mkdir()
    config_path = _write_config(
        tmp_path,
        "schema_version: '6.2'\n"
        "minimum_version: '6.2'\n"
        "tracked_files:\n"
        "  tools: {src: tools, dst: /tmp/root, tree: {}}\n"
        "  nested: {src: nested, dst: /tmp/root/nested}\n"
        "profiles: {p: {tracked_files: [tools, nested]}}\n",
    )
    config = load_config(config_path)
    with pytest.raises(ConfigError, match="managed tree destination overlap"):
        resolve_and_expand(config, "p", tmp_path)


def test_tree_capture_and_named_stage_refuse_one_way_output(tmp_path: Path) -> None:
    source = tmp_path / "tracked" / "tools"
    source.mkdir(parents=True)
    live = tmp_path / "live"
    config_path = _write_config(
        tmp_path,
        "schema_version: '6.2'\n"
        "minimum_version: '6.2'\n"
        "tracked_files:\n"
        f"  tools: {{src: tools, dst: {live}, tree: {{}}}}\n"
        "profiles: {p: {tracked_files: [tools]}}\n",
    )
    config = load_config(config_path)
    resolved = resolve_and_expand(config, "p", tmp_path)

    with pytest.raises(InvariantViolation, match="one-way output"):
        preview_capture_profile(config, "p", tmp_path, resolved=resolved)
    with pytest.raises(typer.BadParameter, match="one-way output"):
        _refuse_generated_stage_target(config, resolved, "tools")
