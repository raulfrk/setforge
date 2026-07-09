"""Themed-widget prompt helpers in ``setforge.cli.config``.

Covers ``_prompt_confirm`` (button_bar) and ``_prompt_marketplace_kind``
(button_bar + text_prompt) — in particular the :data:`CANCEL`-sentinel
paths introduced when the radiolist / input dialogs were replaced with the
themed ``button_bar`` / ``text_prompt`` widgets. The widgets return their
chosen value (or ``CANCEL``) directly — there is no ``.run()`` indirection —
so the fakes here are plain callables patched onto the live module object.
"""

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


# ---------------------------------------------------------------------------
# _prompt_confirm — button_bar
# ---------------------------------------------------------------------------


def test_prompt_confirm_yes_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    # No TTY needed; ``yes`` returns True without touching the widget.
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
    """Esc on the button bar returns CANCEL → treated as abort (no write)."""
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


# ---------------------------------------------------------------------------
# _prompt_marketplace_kind — button_bar + text_prompt
# ---------------------------------------------------------------------------


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
    """Esc at the source-kind bar → CANCEL → SetforgeError abort."""
    _patch_button_bar(monkeypatch, CANCEL)
    with pytest.raises(SetforgeError, match="no source picked"):
        config_mod._prompt_marketplace_kind()


def test_marketplace_kind_cancel_at_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Esc at the repo-slug text prompt → CANCEL → SetforgeError abort."""
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.GITHUB.value)
    _patch_text_prompt(monkeypatch, CANCEL)
    with pytest.raises(SetforgeError, match="no repo entered"):
        config_mod._prompt_marketplace_kind()


def test_marketplace_kind_empty_repo_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank submit (empty string, distinct from CANCEL) still aborts."""
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.GITHUB.value)
    _patch_text_prompt(monkeypatch, "")
    with pytest.raises(SetforgeError, match="no repo entered"):
        config_mod._prompt_marketplace_kind()


def test_marketplace_kind_cancel_at_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Esc at the path text prompt → CANCEL → SetforgeError abort."""
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.PATH.value)
    _patch_text_prompt(monkeypatch, CANCEL)
    with pytest.raises(SetforgeError, match="no path entered"):
        config_mod._prompt_marketplace_kind()


def test_marketplace_kind_empty_path_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_button_bar(monkeypatch, MarketplaceSourceKind.PATH.value)
    _patch_text_prompt(monkeypatch, "")
    with pytest.raises(SetforgeError, match="no path entered"):
        config_mod._prompt_marketplace_kind()
