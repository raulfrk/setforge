"""Helpers shared by the project-profile test modules."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def _git(path: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=check,
        text=True,
        capture_output=True,
    ).stdout


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def _config(tmp_path: Path) -> Path:
    """Create a config repository whose ``demo`` profile injects ``AGENTS.md``."""
    config_root = tmp_path / "config"
    source = config_root / "project" / "demo"
    source.mkdir(parents=True)
    (source / "AGENTS.md").write_text("managed\n")
    (source / "AGENTS.md").chmod(0o644)
    config = config_root / "setforge.yaml"
    config.write_text(
        "tracked_files: {}\nprofiles: {}\nproject_profiles:\n  demo:\n"
        "    files:\n      agents:\n        src: AGENTS.md\n        dst: AGENTS.md\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(config_root)], check=True)
    return config


def _write_older_record(record: Path, schema: int) -> bytes:
    """Rewrite a current record as format 1 or 2 wrote it; return its bytes."""
    document = json.loads(record.read_text())
    document["schema"] = schema
    for entry in document["files"]:
        del entry["visibility"]
        if schema == 1:
            for field in ("applied_payload", "upstream_payload", "upstream_mode"):
                del entry[field]
    if schema == 1:
        del document["config_path"]
    record.write_text(json.dumps(document))
    return record.read_bytes()


def _file_state(path: Path) -> tuple[bytes, int] | None:
    if not path.exists():
        return None
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _private_files(state: Path) -> dict[Path, tuple[bytes, int] | None]:
    return {
        path.relative_to(state): _file_state(path)
        for path in state.rglob("*")
        if path.is_file() and "locks" not in path.relative_to(state).parts
    }


@pytest.fixture(autouse=True)
def candidate_filter_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make Git filter children run this source tree; a module opts in by import."""
    binary_dir = tmp_path / "candidate-bin"
    binary_dir.mkdir()
    entrypoint = binary_dir / "setforge"
    entrypoint.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "if os.environ.get('MUTANT_UNDER_TEST') == 'stats':\n"
        "    os.environ['MUTANT_UNDER_TEST'] = ''\n"
        "_original_cwd = os.getcwd()\n"
        "try:\n"
        f"    os.chdir({str(Path(__file__).parents[1])!r})\n"
        "    from setforge.cli import main\n"
        "finally:\n"
        "    os.chdir(_original_cwd)\n"
        "main()\n"
    )
    entrypoint.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1]))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
