"""CLI-level tests for the ``install`` command's plumbing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge import deploy
from setforge.cli import app
from setforge.deploy import DeployAction, DeployResult


def test_install_passes_precomputed_live_sections_to_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``install`` must pre-extract live user-sections per tracked_file and
    forward them to every ``deploy.copy_atomic`` call via
    ``precomputed_live_sections``.

    Seeds a single-tracked_file profile with ``preserve_user_sections=True``
    plus an existing live file that contains one host-local section,
    then patches ``deploy.copy_atomic`` to capture the kwargs.
    """
    src = tmp_path / "tracked" / "section.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "header\n"
        "<!-- my-setup:user-section start host-local s -->\n"
        f"<!-- my-setup:user-section end host-local s hash={'a' * 64} -->\n"
        "footer\n",
        encoding="utf-8",
    )
    dst = tmp_path / "live" / "section.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        "old header\n"
        "<!-- my-setup:user-section start host-local s -->\n"
        "USER BODY\n"
        f"<!-- my-setup:user-section end host-local s hash={'a' * 64} -->\n"
        "old footer\n",
        encoding="utf-8",
    )

    cfg = tmp_path / "my_setup.yaml"
    cfg.write_text(
        "version: 1\n"
        "tracked_files:\n"
        "  d:\n"
        "    src: section.md\n"
        f"    dst: {dst}\n"
        "    preserve_user_sections: true\n"
        "profiles:\n"
        "  p:\n"
        "    tracked_files: [d]\n",
        encoding="utf-8",
    )

    # Stub out side effects so the test doesn't write transition state.
    monkeypatch.setattr("setforge.vscode_extensions.resolve_binary", lambda _: None)
    monkeypatch.setattr("setforge.transitions.ensure_state_dir_writable", lambda: None)
    monkeypatch.setattr(
        "setforge.transitions.write_transition", lambda *a, **kw: tmp_path / "fake"
    )

    captured: dict[str, Any] = {}

    def _fake_copy_atomic(_src: Path, _dst: Path, **kwargs: Any) -> DeployResult:
        captured.update(kwargs)
        return DeployResult(dst=_dst, action=DeployAction.NOOP, backup_path=None)

    monkeypatch.setattr(deploy, "copy_atomic", _fake_copy_atomic)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["install", "--profile=p", f"--config={cfg}"],
    )
    assert result.exit_code == 0, result.output

    precomputed = captured.get("precomputed_live_sections")
    assert precomputed is not None
    assert isinstance(precomputed, dict)
    assert precomputed.get("s") == "USER BODY\n"
