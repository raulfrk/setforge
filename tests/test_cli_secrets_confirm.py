"""Tests for setforge.cli._secrets_confirm — pre-deploy secret-finding wizard."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from setforge.cli import _secrets_confirm
from setforge.secrets import SecretAction, SecretFinding
from setforge.ui.widgets import CANCEL


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


class _DialogRecorder:
    def __init__(self, *, return_value: object) -> None:
        self._return_value = return_value
        self.call_count = 0
        self.last_kwargs: dict[str, Any] | None = None

    def __call__(self, *_args: Any, **kwargs: Any) -> object:
        self.call_count += 1
        self.last_kwargs = kwargs
        return self._return_value


def _patch_dialog(
    monkeypatch: pytest.MonkeyPatch, *, return_value: object
) -> _DialogRecorder:
    """Install a ``_DialogRecorder`` at the lazy-import seam."""
    recorder = _DialogRecorder(return_value=return_value)
    monkeypatch.setattr("setforge.cli._secrets_confirm.button_bar", recorder)
    return recorder


def _force_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``sys.stdin.isatty()`` return True so the wizard branch fires."""
    monkeypatch.setattr(
        _secrets_confirm.sys.stdin, "isatty", lambda: True, raising=False
    )


# ---------------------------------------------------------------------------
# Panel construction / render (box.frame conversion)
# ---------------------------------------------------------------------------


def test_render_panel_frames_heading_and_body_without_raising() -> None:
    """``_render_panel`` must build and print the framed box without crashing.

    Scripting the button_bar callback (the tests above) bypasses this render
    path entirely, so a real box.frame construction crash would go unseen.
    This exercises the actual frame + theme-style + print pipeline and asserts
    the framed output is a non-empty ``┌─┐`` box carrying the warning heading
    and every body field.
    """
    finding = _make_finding()
    buf = io.StringIO()
    console = Console(file=buf, width=100)

    _secrets_confirm._render_panel(finding, console)

    out = buf.getvalue()
    assert out.strip() != ""  # something was actually rendered
    for corner in ("┌", "┐", "└", "┘"):
        assert corner in out  # a full ┌─┐ box was drawn
    assert "POTENTIAL SECRET DETECTED" in out  # warning heading survives
    assert finding.secret_kind in out  # rule/kind body field
    assert f":{finding.line_number}" in out  # file:line body field
    assert finding.snippet in out  # snippet body field


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


def test_dialog_cancel_treated_as_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_tty(monkeypatch)
    _patch_dialog(monkeypatch, return_value=CANCEL)

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


def test_button_bar_attribute_resolves_lazily() -> None:
    obj = _secrets_confirm.button_bar
    assert callable(obj)


def test_button_bar_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError):
        _ = _secrets_confirm.nonexistent_attribute


# Real-widget construction (no monkeypatch) — see tests/test_ui_widgets.py.


def test_real_widget_enter_selects_initial_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_tty(monkeypatch)
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\r")
        with create_app_session(input=pipe, output=DummyOutput()):
            action = _secrets_confirm.prompt_secret_action(_make_finding())
    assert action is SecretAction.ABORT


def test_real_widget_escape_returns_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_tty(monkeypatch)
    with create_pipe_input() as pipe:
        pipe.send_bytes(b"\x1b")
        with create_app_session(input=pipe, output=DummyOutput()):
            action = _secrets_confirm.prompt_secret_action(_make_finding())
    assert action is SecretAction.ABORT
