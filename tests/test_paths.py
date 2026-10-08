"""Tests for OS-conditional path resolution."""

from pathlib import Path

import pytest

from setforge import paths
from setforge.claude_marketplace_cache import marketplace_cache_root


def _use_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str, home: str, **env: str
) -> None:
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(home)))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_vscode_user_dir_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_platform(monkeypatch, "linux", "/home/test")
    assert paths.vscode_user_dir() == Path("/home/test/.config/Code")


@pytest.mark.parametrize(
    ("xdg_config_home", "expected"),
    [
        ("", "/home/test/.config/Code"),
        ("   ", "/home/test/.config/Code"),
        ("/srv/cfg", "/srv/cfg/Code"),
        ("/srv/cfg/", "/srv/cfg/Code"),
        (" /srv/cfg ", "/srv/cfg/Code"),
        ("relative/cfg", "relative/cfg/Code"),
    ],
)
def test_vscode_user_dir_linux_honours_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, xdg_config_home: str, expected: str
) -> None:
    _use_platform(monkeypatch, "linux", "/home/test", XDG_CONFIG_HOME=xdg_config_home)
    assert paths.vscode_user_dir() == Path(expected)


def test_vscode_user_dir_macos_ignores_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_platform(monkeypatch, "darwin", "/Users/test", XDG_CONFIG_HOME="/srv/cfg")
    assert paths.vscode_user_dir() == Path(
        "/Users/test/Library/Application Support/Code"
    )


@pytest.mark.parametrize(
    ("platform", "home", "xdg_cache_home", "expected"),
    [
        ("linux", "/home/test", None, "/home/test/.cache"),
        ("linux", "/home/test", "", "/home/test/.cache"),
        ("linux", "/home/test", "  ", "/home/test/.cache"),
        ("linux", "/home/test", "/srv/cache", "/srv/cache"),
        ("linux", "/home/test", " /srv/cache/ ", "/srv/cache"),
        ("darwin", "/Users/test", None, "/Users/test/Library/Caches"),
        ("darwin", "/Users/test", "", "/Users/test/Library/Caches"),
        ("darwin", "/Users/test", "/srv/cache", "/srv/cache"),
    ],
)
def test_xdg_cache_home(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    home: str,
    xdg_cache_home: str | None,
    expected: str,
) -> None:
    env = {} if xdg_cache_home is None else {"XDG_CACHE_HOME": xdg_cache_home}
    _use_platform(monkeypatch, platform, home, **env)
    assert paths.xdg_cache_home() == Path(expected)


@pytest.mark.parametrize(
    ("platform", "home", "xdg_cache_home", "expected"),
    [
        ("linux", "/home/test", None, "/home/test/.cache/setforge/marketplaces"),
        ("linux", "/home/test", "/srv/cache", "/srv/cache/setforge/marketplaces"),
        (
            "darwin",
            "/Users/test",
            None,
            "/Users/test/Library/Caches/setforge/marketplaces",
        ),
        ("darwin", "/Users/test", "/srv/cache", "/srv/cache/setforge/marketplaces"),
    ],
)
def test_marketplace_cache_root(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    home: str,
    xdg_cache_home: str | None,
    expected: str,
) -> None:
    env = {} if xdg_cache_home is None else {"XDG_CACHE_HOME": xdg_cache_home}
    _use_platform(monkeypatch, platform, home, **env)
    assert marketplace_cache_root() == Path(expected)


def test_vscode_user_dir_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/test")))
    expected = Path("/Users/test/Library/Application Support/Code")
    assert paths.vscode_user_dir() == expected


def test_template_context_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_platform(monkeypatch, "linux", "/home/test")
    ctx = paths.template_context()
    assert ctx["vscode_user_dir"] == "/home/test/.config/Code/User"
    assert ctx["home"] == "/home/test"


def test_template_context_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/test")))
    ctx = paths.template_context()
    assert ctx["vscode_user_dir"] == "/Users/test/Library/Application Support/Code/User"
    assert ctx["home"] == "/Users/test"
