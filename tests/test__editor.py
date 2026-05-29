"""Unit tests for setforge._editor.run_editor."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from setforge import _editor
from setforge._editor import run_editor
from setforge.errors import SetforgeError


def test_missing_editor_raises_mysetuperror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EDITOR", "nonexistent-binary-xyz-987")
    target = tmp_path / "file.md"
    with pytest.raises(SetforgeError, match=r"not found on PATH"):
        run_editor(target)


def test_empty_editor_raises_mysetuperror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EDITOR", "")
    target = tmp_path / "file.md"
    with pytest.raises(SetforgeError, match=r"\$EDITOR is empty"):
        run_editor(target)


def test_multitoken_editor_passes_to_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EDITOR", "echo --wait")
    captured: dict[str, list[str]] = {}

    def fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_editor.subprocess, "run", fake_run)
    target = tmp_path / "file.md"
    run_editor(target)
    assert captured["argv"] == ["echo", "--wait", str(target)]


def test_run_editor_wraps_shlex_split_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EDITOR", "vi 'unclosed")
    target = tmp_path / "f.txt"
    target.touch()
    with pytest.raises(SetforgeError, match="malformed quoting") as excinfo:
        run_editor(target)
    assert isinstance(excinfo.value.__cause__, ValueError), (
        "expected SetforgeError to chain via `from exc` to preserve shlex column info"
    )


def test_editor_run_passes_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The editor subprocess.run call must carry ``timeout=_EDITOR_TIMEOUT_S``."""
    monkeypatch.setenv("EDITOR", "echo")
    captured: dict[str, object] = {}

    def fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_editor.subprocess, "run", fake_run)
    target = tmp_path / "file.md"
    run_editor(target)
    assert captured["timeout"] == _editor._EDITOR_TIMEOUT_S


def test_editor_timeout_surfaces_setforge_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hung editor (TimeoutExpired) surfaces SetforgeError chained from exc."""
    monkeypatch.setenv("EDITOR", "echo")

    def fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=3600)

    monkeypatch.setattr(_editor.subprocess, "run", fake_run)
    target = tmp_path / "file.md"
    with pytest.raises(SetforgeError) as excinfo:
        run_editor(target)
    assert isinstance(excinfo.value.__cause__, subprocess.TimeoutExpired), (
        "expected SetforgeError chained via `from exc` from TimeoutExpired"
    )


def test_default_to_vi_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EDITOR", raising=False)
    captured: dict[str, list[str]] = {}

    def fake_which(name: str) -> str | None:
        return "/usr/bin/vi" if name == "vi" else None

    def fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_editor.shutil, "which", fake_which)
    monkeypatch.setattr(_editor.subprocess, "run", fake_run)
    target = tmp_path / "file.md"
    run_editor(target)
    assert captured["argv"] == ["vi", str(target)]
