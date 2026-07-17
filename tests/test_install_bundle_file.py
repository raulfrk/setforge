"""Integration: a bundle ``file`` component deploys via the tracked-file path."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from setforge.cli import app

_PROFILE = "bundle-file-test"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SETFORGE_STATE_DIR", str(tmp_path / "state"))
    target = tmp_path / "repo"
    target.mkdir()
    return target


def _write_launcher(repo: Path, body: str = "#!/bin/sh\necho hi\n") -> None:
    tracked = repo / "tracked"
    tracked.mkdir(parents=True, exist_ok=True)
    (tracked / "launch.sh").write_text(body, encoding="utf-8")


def _write_config(repo: Path) -> Path:
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files: {}\n"
        "bundles:\n"
        "  revdiff:\n"
        "    components:\n"
        "      - id: launcher\n"
        "        file:\n"
        "          src: launch.sh\n"
        "          dst: ~/.local/share/rd/launch.sh\n"
        "          mode: 0o755\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    bundles:\n"
        "      - revdiff\n",
        encoding="utf-8",
    )
    return config


def _write_config_no_bundle(repo: Path) -> Path:
    (repo / "tracked").mkdir(parents=True, exist_ok=True)
    (repo / "tracked" / "note.md").write_text("hello\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.md\n"
        "    dst: ~/.local/share/rd/note.md\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - note\n",
        encoding="utf-8",
    )
    return config


def _launcher_live() -> Path:
    return Path.home() / ".local" / "share" / "rd" / "launch.sh"


def _install(config: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "install",
            f"--profile={_PROFILE}",
            f"--config={config}",
            "--no-secrets-scan",
            "--no-git-check",
            "--yes",
            *extra,
        ],
    )


def test_bundle_file_deploys_with_mode(repo: Path) -> None:
    _write_launcher(repo)
    config = _write_config(repo)
    result = _install(config)
    assert result.exit_code == 0, result.output
    live = _launcher_live()
    assert live.exists()
    assert live.read_text(encoding="utf-8") == "#!/bin/sh\necho hi\n"
    mode = stat.S_IMODE(live.stat().st_mode)
    assert mode == 0o755, oct(mode)
    assert mode & stat.S_IXUSR, "launcher must be executable"


def test_bundle_file_hand_edit_survives_reinstall(repo: Path) -> None:
    _write_launcher(repo)
    config = _write_config(repo)
    assert _install(config).exit_code == 0
    live = _launcher_live()
    live.write_text("#!/bin/sh\necho EDITED\n", encoding="utf-8")
    assert _install(config).exit_code == 0
    assert "EDITED" in live.read_text(encoding="utf-8")


def test_no_bundle_file_profile_unchanged(repo: Path) -> None:
    config = _write_config_no_bundle(repo)
    result = _install(config)
    assert result.exit_code == 0, result.output
    live = Path.home() / ".local" / "share" / "rd" / "note.md"
    assert live.read_text(encoding="utf-8") == "hello\n"
    assert not _launcher_live().exists()


def test_install_refuses_out_of_home_dst(repo: Path) -> None:
    _write_launcher(repo)
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files: {}\n"
        "bundles:\n"
        "  revdiff:\n"
        "    components:\n"
        "      - id: launcher\n"
        "        file:\n"
        "          src: launch.sh\n"
        "          dst: ~/../etc/evil\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    bundles:\n"
        "      - revdiff\n",
        encoding="utf-8",
    )
    result = _install(config)
    assert result.exit_code != 0, result.output


def test_install_warns_out_of_home_dst_plain_tracked_file(repo: Path) -> None:
    (repo / "tracked").mkdir(parents=True, exist_ok=True)
    (repo / "tracked" / "note.md").write_text("pwn\n", encoding="utf-8")
    evil = Path.home().resolve().parent / "etc" / "cron.d" / "pwn"
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.md\n"
        "    dst: ~/../etc/cron.d/pwn\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - note\n",
        encoding="utf-8",
    )
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "outside $HOME" in result.output
    assert evil.exists()
    assert evil.read_text(encoding="utf-8") == "pwn\n"


def test_install_allow_outside_home_deploys_silently(repo: Path) -> None:
    (repo / "tracked").mkdir(parents=True, exist_ok=True)
    (repo / "tracked" / "note.md").write_text("pwn\n", encoding="utf-8")
    evil = Path.home().resolve().parent / "etc" / "cron.d" / "pwn"
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  note:\n"
        "    src: note.md\n"
        "    dst: ~/../etc/cron.d/pwn\n"
        "    allow_outside_home: true\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - note\n",
        encoding="utf-8",
    )
    result = _install(config)
    assert result.exit_code == 0, result.output
    assert "outside $HOME" not in result.output
    assert evil.exists()
    assert evil.read_text(encoding="utf-8") == "pwn\n"


def test_install_refuses_name_collision(repo: Path) -> None:
    _write_launcher(repo)
    (repo / "tracked" / "real.md").write_text("real body\n", encoding="utf-8")
    config = repo / "setforge.yaml"
    config.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  revdiff.launcher:\n"
        "    src: real.md\n"
        "    dst: ~/.local/share/rd/real.md\n"
        "bundles:\n"
        "  revdiff:\n"
        "    components:\n"
        "      - id: launcher\n"
        "        file:\n"
        "          src: launch.sh\n"
        "          dst: ~/.local/share/rd/launch.sh\n"
        "profiles:\n"
        f"  {_PROFILE}:\n"
        "    tracked_files:\n"
        "      - revdiff.launcher\n"
        "    bundles:\n"
        "      - revdiff\n",
        encoding="utf-8",
    )
    result = _install(config)
    assert result.exit_code != 0, result.output
    live_real = Path.home() / ".local" / "share" / "rd" / "real.md"
    assert not live_real.exists()
