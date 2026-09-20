from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import release_preflight


def test_installed_checks_share_isolated_uv_tool_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "setforge-preflight-test"
    root.mkdir()
    monkeypatch.setattr(
        release_preflight.tempfile,
        "mkdtemp",
        lambda **_kwargs: str(root),
    )
    monkeypatch.setattr(release_preflight.atexit, "register", lambda *_a, **_kw: None)
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def _run(
        *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert env is not None
        calls.append((args, env))
        stdout = (
            "1.3.4"
            if "--version" in args
            else " ".join(release_preflight._REQUIRED_COMMANDS)
        )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(release_preflight, "_run", _run)

    installed_root = release_preflight.step_3_install_in_tmp("1.3.4")
    release_preflight.step_4_installed_version(installed_root, "1.3.4")
    release_preflight.step_5_installed_help(installed_root)

    assert installed_root == root
    assert len(calls) == 3
    for _, env in calls:
        assert env["UV_TOOL_DIR"] == str(root / "tools")
        assert env["UV_TOOL_BIN_DIR"] == str(root / "bin")
