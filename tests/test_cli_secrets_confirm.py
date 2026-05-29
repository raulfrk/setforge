"""Tests for setforge.cli._secrets_confirm — pre-deploy secret-finding wizard."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from setforge.cli import _secrets_confirm
from setforge.secrets import SecretAction, SecretFinding


def _hash(text: str) -> str:
    """Helper: hex sha256 of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_finding(snippet: str = "ghp_xxxxxxxxxxxx") -> SecretFinding:
    """Construct a representative SecretFinding."""
    return SecretFinding(
        rule_id="github-pat",
        file_path=Path("tracked/claude/skills/foo/SKILL.md"),
        line_number=42,
        snippet=snippet,
        snippet_hash=_hash(snippet),
        secret_kind="GitHub Personal Access Token",
    )


class _FakeDialogResult:
    """Stand-in for prompt_toolkit's ``Dialog`` return object.

    Mirrors the pattern in ``tests/test_cli_auto_confirm.py`` so the
    seam shape is consistent across wizard tests.
    """

    def __init__(self, *, return_value: object) -> None:
        self._return_value = return_value

    def run(self) -> object:
        return self._return_value


class _DialogRecorder:
    """Callable replacing ``radiolist_dialog`` to record + control returns."""

    def __init__(self, *, return_value: object) -> None:
        self.fake = _FakeDialogResult(return_value=return_value)
        self.call_count = 0
        self.last_kwargs: dict[str, Any] | None = None

    def __call__(self, *_args: Any, **kwargs: Any) -> _FakeDialogResult:
        self.call_count += 1
        self.last_kwargs = kwargs
        return self.fake


def _patch_dialog(
    monkeypatch: pytest.MonkeyPatch, *, return_value: object
) -> _DialogRecorder:
    """Install a ``_DialogRecorder`` at the lazy-import seam."""
    recorder = _DialogRecorder(return_value=return_value)
    monkeypatch.setattr("setforge.cli._secrets_confirm.radiolist_dialog", recorder)
    return recorder


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``sys.stdin.isatty()`` return True so the wizard branch fires."""
    monkeypatch.setattr(
        _secrets_confirm.sys.stdin, "isatty", lambda: True, raising=False
    )


# ---------------------------------------------------------------------------
# Dialog return mappings
# ---------------------------------------------------------------------------


def test_dialog_abort_returns_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """User picks ABORT → wizard returns SecretAction.ABORT."""
    _force_tty(monkeypatch)
    recorder = _patch_dialog(monkeypatch, return_value=SecretAction.ABORT)

    action = _secrets_confirm.prompt_secret_action(_make_finding())

    assert action is SecretAction.ABORT
    assert recorder.call_count == 1


def test_dialog_allowlist_returns_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User picks ALLOWLIST → wizard returns SecretAction.ALLOWLIST."""
    _force_tty(monkeypatch)
    _patch_dialog(monkeypatch, return_value=SecretAction.ALLOWLIST)

    action = _secrets_confirm.prompt_secret_action(_make_finding())

    assert action is SecretAction.ALLOWLIST


def test_dialog_silence_one_shot_returns_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User picks SILENCE_ONE_SHOT → wizard returns the matching enum value."""
    _force_tty(monkeypatch)
    _patch_dialog(monkeypatch, return_value=SecretAction.SILENCE_ONE_SHOT)

    action = _secrets_confirm.prompt_secret_action(_make_finding())

    assert action is SecretAction.SILENCE_ONE_SHOT


def test_dialog_none_treated_as_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Esc / Ctrl-C (dialog returns None) maps to ABORT (mockup-T default)."""
    _force_tty(monkeypatch)
    _patch_dialog(monkeypatch, return_value=None)

    action = _secrets_confirm.prompt_secret_action(_make_finding())

    assert action is SecretAction.ABORT


# ---------------------------------------------------------------------------
# Non-interactive short-circuits
# ---------------------------------------------------------------------------


def test_yes_short_circuits_to_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """yes=True must NOT auto-bypass a finding; returns ABORT without dialog."""
    recorder = _patch_dialog(monkeypatch, return_value=SecretAction.ALLOWLIST)

    action = _secrets_confirm.prompt_secret_action(_make_finding(), yes=True)

    assert action is SecretAction.ABORT
    assert recorder.call_count == 0


def test_non_tty_returns_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a TTY, the wizard returns ABORT without invoking the dialog."""
    monkeypatch.setattr(
        _secrets_confirm.sys.stdin, "isatty", lambda: False, raising=False
    )
    recorder = _patch_dialog(monkeypatch, return_value=SecretAction.ALLOWLIST)

    action = _secrets_confirm.prompt_secret_action(_make_finding())

    assert action is SecretAction.ABORT
    assert recorder.call_count == 0


def test_non_tty_emits_stderr_warning_and_aborts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-TTY (without ``yes``) aborts AND surfaces a stderr warning.

    A silent ABORT in a non-interactive pipeline hides WHY the install
    stopped; the wizard must explain that a secret was found and there
    is no TTY to confirm. The warning must NOT reproduce the candidate
    secret value — echoing the snippet would leak it into stderr / CI
    logs / scrollback, defeating the very abort that protects it.
    """
    monkeypatch.setattr(
        _secrets_confirm.sys.stdin, "isatty", lambda: False, raising=False
    )
    recorder = _patch_dialog(monkeypatch, return_value=SecretAction.ALLOWLIST)
    secret = "ghp_DEADBEEFsentinel0123456789"

    action = _secrets_confirm.prompt_secret_action(_make_finding(snippet=secret))

    assert action is SecretAction.ABORT
    assert recorder.call_count == 0
    err = capsys.readouterr().err
    assert err.strip() != ""  # a non-empty diagnostic, not a silent abort
    assert secret not in err  # the secret value must never leak to stderr


def test_yes_short_circuit_emits_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ``yes=True`` ABORT is the documented defense-in-depth path —
    it is intentional, not a missing-TTY condition, so it must stay
    silent. A 'no TTY' diagnostic here would be spurious noise on the
    automation path (the install caller already prints its own abort
    message).
    """
    monkeypatch.setattr(
        _secrets_confirm.sys.stdin, "isatty", lambda: False, raising=False
    )
    recorder = _patch_dialog(monkeypatch, return_value=SecretAction.ALLOWLIST)

    action = _secrets_confirm.prompt_secret_action(_make_finding(), yes=True)

    assert action is SecretAction.ABORT
    assert recorder.call_count == 0
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Lazy import seam
# ---------------------------------------------------------------------------


def test_radiolist_dialog_attribute_resolves_lazily() -> None:
    """The module-level ``__getattr__`` exposes ``radiolist_dialog`` on demand."""
    obj = _secrets_confirm.radiolist_dialog
    assert callable(obj)


def test_radiolist_dialog_unknown_attribute_raises() -> None:
    """``__getattr__`` raises ``AttributeError`` for unknown names."""
    with pytest.raises(AttributeError):
        _ = _secrets_confirm.nonexistent_attribute
