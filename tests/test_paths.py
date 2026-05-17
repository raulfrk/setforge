"""Tests for OS-conditional path resolution."""

from pathlib import Path

import pytest

from setforge import paths


def test_vscode_user_dir_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
    monkeypatch.setattr(
        "platformdirs.user_config_path",
        lambda name: Path(f"/home/test/.config/{name}"),
    )
    assert paths.vscode_user_dir() == Path("/home/test/.config/Code")


def test_vscode_user_dir_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/test")))
    expected = Path("/Users/test/Library/Application Support/Code")
    assert paths.vscode_user_dir() == expected


def test_template_context_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/test")))
    monkeypatch.setattr(
        "platformdirs.user_config_path",
        lambda name: Path(f"/home/test/.config/{name}"),
    )
    ctx = paths.template_context()
    assert ctx["vscode_user_dir"] == "/home/test/.config/Code/User"
    assert ctx["home"] == "/home/test"


def test_template_context_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/test")))
    ctx = paths.template_context()
    assert ctx["vscode_user_dir"] == "/Users/test/Library/Application Support/Code/User"
    assert ctx["home"] == "/Users/test"
