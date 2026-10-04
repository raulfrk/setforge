"""``install --dry-run`` reports the refusals a real install raises.

The preview and the apply path build the same plan, so a condition that makes
the real install stop before its first write is named in the preview's
``=== would-be refusal ===`` block with the same message. The preview stays
informational: it exits 0 and writes nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app

_REFUSAL_HEADER = "=== would-be refusal ==="
_FINAL_LINE = "=== rerun without --dry-run to apply for real ==="


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    target = tmp_path / "repo"
    (target / "tracked").mkdir(parents=True)
    return target


def _install(config: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            "--profile=p",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--no-fetch",
            *extra,
        ],
    )


def _refusal_block(output: str) -> list[str]:
    lines = output.splitlines()
    start = lines.index(_REFUSAL_HEADER)
    return lines[start + 1 : lines.index(_FINAL_LINE)]


def _tree_snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mode,
            path.read_bytes() if path.is_file() and not path.is_symlink() else None,
        )
        for path in sorted(root.rglob("*"))
    }


def _symlink_config(repo: Path) -> tuple[Path, Path]:
    (repo / "tracked" / "a.md").write_text("# A\n", encoding="utf-8")
    (repo / "tracked" / "z.md").write_text("# Z\n", encoding="utf-8")
    live = Path.home() / ".live"
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.5'\n"
        "tracked_files:\n"
        "  a:\n"
        "    src: a.md\n"
        f"    dst: {live}/a.md\n"
        "  z:\n"
        "    src: z.md\n"
        f"    dst: {live}/z-link\n"
        f"    symlink: {live}/z-target\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [a, z]\n",
        encoding="utf-8",
    )
    return config, live


@pytest.mark.parametrize("occupant", ["regular file", "directory"])
def test_dry_run_reports_the_occupied_symlink_destination_apply_refuses(
    repo: Path, tmp_path: Path, occupant: str
) -> None:
    config, live = _symlink_config(repo)
    live.mkdir()
    if occupant == "directory":
        (live / "z-link").mkdir()
    else:
        (live / "z-link").write_text("host content\n", encoding="utf-8")
    before = _tree_snapshot(tmp_path)

    preview = _install(config, "--dry-run")
    after_preview = _tree_snapshot(tmp_path)
    applied = _install(config, "--yes")

    assert applied.exit_code != 0
    refusal = str(applied.exception)
    assert refusal == (
        f"refusing to deploy symlink at {live}/z-link: a {occupant} is already "
        "present. Move it aside or remove it before deploying tracked_file with "
        f"symlink: '{live}/z-target'."
    )
    assert preview.exit_code == 0, preview.output
    assert _refusal_block(preview.output) == [f"  {refusal}"]
    assert preview.output.splitlines()[-1] == _FINAL_LINE
    assert after_preview == before
    assert not (live / "a.md").exists()


def _mode_drift_config(repo: Path) -> tuple[Path, Path]:
    (repo / "tracked" / "note.txt").write_text("same\n", encoding="utf-8")
    live = Path.home() / ".live" / "note.txt"
    live.parent.mkdir()
    live.write_text("same\n", encoding="utf-8")
    live.chmod(0o600)
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.5'\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.txt\n"
        f"    dst: {live}\n"
        "    mode: 0o644\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [note]\n",
        encoding="utf-8",
    )
    return config, live


def test_dry_run_reports_the_mode_drift_apply_refuses(
    repo: Path, tmp_path: Path
) -> None:
    config, live = _mode_drift_config(repo)
    before = _tree_snapshot(tmp_path)

    preview = _install(config, "--dry-run")
    after_preview = _tree_snapshot(tmp_path)
    applied = _install(config, "--yes")

    refusal = (
        "permission-mode drift in 1 file(s) (profile 'p'): "
        "pass --auto-accept-tracked or --auto-accept-live to resolve"
    )
    assert applied.exit_code == 1
    assert refusal in applied.output
    assert preview.exit_code == 0, preview.output
    assert "unexpected drift in 1 file(s)" in preview.output
    assert _refusal_block(preview.output) == [f"  {refusal}"]
    assert after_preview == before
    assert live.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("flag", ["--auto-accept-tracked", "--auto-accept-live"])
def test_dry_run_reports_no_mode_drift_refusal_when_a_flag_resolves_it(
    repo: Path, flag: str
) -> None:
    config, live = _mode_drift_config(repo)

    preview = _install(config, "--dry-run", flag)
    applied = _install(config, "--yes", flag)

    assert preview.exit_code == 0, preview.output
    assert "unexpected drift in 1 file(s)" in preview.output
    assert _REFUSAL_HEADER not in preview.output
    assert applied.exit_code == 0, applied.output
    assert live.stat().st_mode & 0o777 == 0o644


def test_dry_run_lists_every_refusal_in_the_order_apply_checks_them(
    repo: Path,
) -> None:
    (repo / "tracked" / "note.txt").write_text("same\n", encoding="utf-8")
    (repo / "tracked" / "z.md").write_text("# Z\n", encoding="utf-8")
    live = Path.home() / ".live"
    live.mkdir()
    (live / "note.txt").write_text("same\n", encoding="utf-8")
    (live / "note.txt").chmod(0o600)
    (live / "z-link").write_text("host content\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.5'\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.txt\n"
        f"    dst: {live}/note.txt\n"
        "    mode: 0o644\n"
        "  z:\n"
        "    src: z.md\n"
        f"    dst: {live}/z-link\n"
        f"    symlink: {live}/z-target\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [note, z]\n",
        encoding="utf-8",
    )

    preview = _install(config, "--dry-run")
    applied = _install(config, "--yes")
    accepted = _install(config, "--yes", "--auto-accept-tracked")

    block = _refusal_block(preview.output)
    assert [line.split(":")[0] for line in block] == [
        "  permission-mode drift in 1 file(s) (profile 'p')",
        f"  refusing to deploy symlink at {live}/z-link",
    ]
    assert applied.exit_code == 1
    assert block[0].strip() in applied.output
    assert accepted.exit_code != 0
    assert str(accepted.exception) == block[1].strip()


def test_clean_dry_run_has_no_refusal_block(repo: Path) -> None:
    (repo / "tracked" / "note.txt").write_text("same\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "schema_version: '6.5'\n"
        "minimum_version: '6.5'\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.txt\n"
        f"    dst: {Path.home()}/.live/note.txt\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [note]\n",
        encoding="utf-8",
    )

    preview = _install(config, "--dry-run")

    assert preview.exit_code == 0, preview.output
    assert _REFUSAL_HEADER not in preview.output
    assert preview.output.splitlines()[-1] == _FINAL_LINE
