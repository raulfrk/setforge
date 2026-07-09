from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from setforge.cli import config as config_mod
from setforge.config import MarketplaceSourceKind
from setforge.errors import SetforgeError
from setforge.ui.widgets import CANCEL


def _patch_button_bar(monkeypatch: pytest.MonkeyPatch, return_value: object) -> None:
    def _fake(*_args: Any, **_kwargs: Any) -> object:
        return return_value

    monkeypatch.setattr(config_mod, "button_bar", _fake)


def _patch_text_prompt(monkeypatch: pytest.MonkeyPatch, *values: object) -> None:
    seq = iter(values)

    def _fake(*_args: Any, **_kwargs: Any) -> object:
        return next(seq)

    monkeypatch.setattr(config_mod, "text_prompt", _fake)


def test_prompt_confirm_yes_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> object:
        raise AssertionError("button_bar must not run when yes=True")

    monkeypatch.setattr(config_mod, "button_bar", _boom)
    assert (
        config_mod._prompt_confirm(
            yaml_path=Path("/x/setforge.yaml"),
            diff_text="+a",
            console=Console(record=True),
            yes=True,
        )
        is True
    )


def test_prompt_confirm_write_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_button_bar(monkeypatch, True)
    console = Console(record=True)
    assert (
        config_mod._prompt_confirm(
            yaml_path=Path("/x/setforge.yaml"),
            diff_text="+a",
            console=console,
            yes=False,
        )
        is True
    )
    assert "writing" in console.export_text()


def test_prompt_confirm_false_button_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_button_bar(monkeypatch, False)
    console = Console(record=True)
    assert (
        config_mod._prompt_confirm(
            yaml_path=Path("/x/setforge.yaml"),
            diff_text="+a",
            console=console,
            yes=False,
        )
        is False
    )
    assert "aborted" in console.export_text()


def test_prompt_confirm_cancel_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    _patch_button_bar(monkeypatch, CANCEL)
    console = Console(record=True)
    assert (
        config_mod._prompt_confirm(
            yaml_path=Path("/x/setforge.yaml"),
            diff_text="+a",
            console=console,
            yes=False,
        )
        is False
    )
    assert "aborted" in console.export_text()


def test_marketplace_kind_github_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.GITHUB.value)
    _patch_text_prompt(monkeypatch, "owner/name")
    kind, repo, path = config_mod._prompt_marketplace_kind()
    assert kind == MarketplaceSourceKind.GITHUB.value
    assert repo == "owner/name"
    assert path is None


def test_marketplace_kind_path_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.PATH.value)
    _patch_text_prompt(monkeypatch, "/abs/clone")
    kind, repo, path = config_mod._prompt_marketplace_kind()
    assert kind == MarketplaceSourceKind.PATH.value
    assert repo is None
    assert path == "/abs/clone"


def test_marketplace_kind_cancel_at_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_button_bar(monkeypatch, CANCEL)
    with pytest.raises(SetforgeError, match="no source picked"):
        config_mod._prompt_marketplace_kind()


def test_marketplace_kind_cancel_at_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.GITHUB.value)
    _patch_text_prompt(monkeypatch, CANCEL)
    with pytest.raises(SetforgeError, match="no repo entered"):
        config_mod._prompt_marketplace_kind()


def test_marketplace_kind_empty_repo_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.GITHUB.value)
    _patch_text_prompt(monkeypatch, "")
    with pytest.raises(SetforgeError, match="no repo entered"):
        config_mod._prompt_marketplace_kind()


def test_marketplace_kind_cancel_at_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.PATH.value)
    _patch_text_prompt(monkeypatch, CANCEL)
    with pytest.raises(SetforgeError, match="no path entered"):
        config_mod._prompt_marketplace_kind()


def test_marketplace_kind_empty_path_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.PATH.value)
    _patch_text_prompt(monkeypatch, "")
    with pytest.raises(SetforgeError, match="no path entered"):
        config_mod._prompt_marketplace_kind()
