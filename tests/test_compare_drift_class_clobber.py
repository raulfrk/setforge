"""Tests for plain markdown drift with no stored base.

A plain markdown file whose live copy diverged from tracked, with no stored
base, is UNEXPECTED drift with no reason note, and ``compare --check`` fails
on it.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.compare import CompareStatus, DriftClass
from setforge.config import Config, Profile, TrackedFile
from tests.verb_calls import compare_profile

_DOC = """\
# Title

## Pinned

Pinned body original.

## Shared

Shared body original.
"""

_DOC_EDITED = _DOC.replace("Pinned body original.", "Pinned body LIVE.")


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(state))
    return state


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _doc_file(tmp_path: Path) -> tuple[Config, Path]:
    """One plain markdown tracked_file; live diverges from tracked with no
    stored base. Returns (config, repo_root)."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "doc.md", _DOC)
    dst = tmp_path / "live" / "doc.md"
    _write(dst, _DOC_EDITED)
    config = Config(
        tracked_files={
            "doc": TrackedFile.model_validate({"src": "doc.md", "dst": str(dst)})
        },
        profiles={"p": Profile(tracked_files=["doc"])},
    )
    return config, repo


def test_live_edit_no_base_is_unexpected(tmp_path: Path) -> None:
    """A live markdown edit with NO stored base is plain UNEXPECTED drift."""
    config, repo = _doc_file(tmp_path)

    report = compare_profile(config, "p", repo)
    entry = report.entries[0]

    assert entry.status is CompareStatus.DRIFTED
    assert entry.drift_class is DriftClass.UNEXPECTED
    assert entry.reason is None
    assert report.has_unexpected_drift is True


def _write_cli_config(tmp_path: Path) -> Path:
    """Write the doc-file scenario as a real setforge.yaml; returns its path."""
    repo = tmp_path / "repo"
    _write(repo / "tracked" / "doc.md", _DOC)
    dst = tmp_path / "live" / "doc.md"
    _write(dst, _DOC_EDITED)
    cfg_path = repo / "setforge.yaml"
    cfg_path.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  doc:\n"
        "    src: doc.md\n"
        f"    dst: {dst}\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [doc]\n",
        encoding="utf-8",
    )
    return cfg_path


def test_check_exits_1_on_unexpected(tmp_path: Path) -> None:
    """compare --check fails on the unexpected markdown drift."""
    cfg_path = _write_cli_config(tmp_path)

    result = CliRunner().invoke(
        app, ["compare", "--profile=p", f"--config={cfg_path}", "--check"]
    )
    assert result.exit_code == 1, result.output
