"""Unit tests for ``setforge completion install`` (mockup K)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from setforge.cli import app
from setforge.cli import completion as completion_mod
from setforge.cli.completion import (
    CompletionChoice,
    ShellKind,
    _detect_wiring,
    _script_path,
    _wrap_sentinel,
    _write_wiring,
)
from setforge.errors import ConfirmRequiresInteractive, SetforgeError

_RUNNER = CliRunner()

_FAKE_ZSH_SCRIPT = "#compdef setforge\n_setforge_completion() { :; }\n"
_FAKE_BASH_SCRIPT = "#!/usr/bin/env bash\n_setforge_completion() { :; }\n"
_FAKE_FISH_SCRIPT = "# fish completion for setforge\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-point ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_show_completion(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace ``subprocess.run`` so ``--show-completion`` returns a fixture body.

    Returns a list that captures every argv passed to ``subprocess.run``
    via the completion module's attribute path; tests can assert on it
    to verify the shell value reached the subprocess.
    """
    captured: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output, text, timeout  # unused in fake
        captured.append(list(argv))
        body_for = {
            "zsh": _FAKE_ZSH_SCRIPT,
            "bash": _FAKE_BASH_SCRIPT,
            "fish": _FAKE_FISH_SCRIPT,
        }
        stdout = body_for.get(argv[-1], "")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("setforge.cli.completion.subprocess.run", fake_run)
    return captured


class _FakeDialogResult:
    """Stand-in for ``radiolist_dialog(...).run()``."""

    def __init__(self, return_value: object) -> None:
        self._return_value = return_value
        self.run_calls = 0

    def run(self) -> object:
        self.run_calls += 1
        return self._return_value


def _stub_dialog(
    monkeypatch: pytest.MonkeyPatch, return_value: object
) -> _FakeDialogResult:
    """Pin ``setforge.cli.completion.radiolist_dialog`` to a canned result.

    Also force ``sys.stdin.isatty`` → True so the mutate-gate inside
    :func:`completion_install` lets the interactive branch run under
    pytest (where the test runner's stdin is non-TTY by default).
    """
    result = _FakeDialogResult(return_value)

    def fake_dialog(**kwargs: Any) -> _FakeDialogResult:
        del kwargs
        return result

    monkeypatch.setattr("setforge.cli.completion.radiolist_dialog", fake_dialog)
    monkeypatch.setattr("setforge.cli.completion._stdin_is_tty", lambda: True)
    return result


# ---------------------------------------------------------------------------
# helpers: idempotency primitives
# ---------------------------------------------------------------------------


def test_detect_wiring_returns_false_for_missing_file(tmp_path: Path) -> None:
    assert _detect_wiring(tmp_path / "nonexistent") is False


def test_detect_wiring_returns_false_when_sentinel_absent(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("# user content\nexport FOO=1\n")
    assert _detect_wiring(rc) is False


def test_detect_wiring_returns_true_when_sentinel_block_present(
    tmp_path: Path,
) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("# user content\n" + _wrap_sentinel("fpath=(...)\n"))
    assert _detect_wiring(rc) is True


def test_write_wiring_refuses_when_rc_file_missing(tmp_path: Path) -> None:
    with pytest.raises(SetforgeError, match="rc file not found"):
        _write_wiring(tmp_path / "missing", "body\n")


def test_write_wiring_appends_when_sentinel_absent(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("# user content\nexport FOO=1\n")
    _write_wiring(rc, "fpath=(test)\n")
    text = rc.read_text()
    assert "# user content" in text
    assert "export FOO=1" in text
    assert "# >>> setforge completion >>>" in text
    assert "fpath=(test)" in text
    assert "# <<< setforge completion <<<" in text


def test_write_wiring_replaces_existing_sentinel_block(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("user line\n" + _wrap_sentinel("old body\n") + "trailing\n")
    _write_wiring(rc, "new body\n")
    text = rc.read_text()
    assert "old body" not in text
    assert "new body" in text
    assert text.count("# >>> setforge completion >>>") == 1
    assert "user line" in text
    assert "trailing" in text


def test_write_wiring_idempotent_second_call_same_body(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("user line\n")
    _write_wiring(rc, "fpath body\n")
    first = rc.read_text()
    _write_wiring(rc, "fpath body\n")
    assert rc.read_text() == first


def test_write_wiring_ensures_trailing_newline_before_block(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("noeol")  # no trailing newline
    _write_wiring(rc, "body\n")
    text = rc.read_text()
    assert text.startswith("noeol\n")


# ---------------------------------------------------------------------------
# completion install: zsh (mockup K)
# ---------------------------------------------------------------------------


def test_completion_install_zsh_virgin_writes_files_and_appends_rc(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".zshrc"
    rc.write_text("# user content\nalias ls=ls\n")
    _stub_dialog(monkeypatch, CompletionChoice.YES_AND_WIRE)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh"])

    assert result.exit_code == 0, result.output
    assert (home / ".config/setforge/completions/_setforge").read_text() == (
        _FAKE_ZSH_SCRIPT
    )
    text = rc.read_text()
    assert "fpath=" in text
    assert "compinit" in text
    assert "# >>> setforge completion >>>" in text
    assert "# user content" in text
    # subprocess.run was called with the correct shell value
    assert any("zsh" in argv for argv in fake_show_completion)


def test_completion_install_zsh_yes_only_skips_rc_edit(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".zshrc"
    rc.write_text("# untouched\n")
    _stub_dialog(monkeypatch, CompletionChoice.YES_ONLY)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh"])

    assert result.exit_code == 0, result.output
    assert (home / ".config/setforge/completions/_setforge").exists()
    assert rc.read_text() == "# untouched\n"


def test_completion_install_zsh_abort_writes_nothing(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".zshrc"
    rc.write_text("# untouched\n")
    _stub_dialog(monkeypatch, CompletionChoice.ABORT)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh"])

    assert result.exit_code == 1, result.output
    assert not (home / ".config/setforge/completions/_setforge").exists()
    assert rc.read_text() == "# untouched\n"


def test_completion_install_zsh_dialog_escape_treated_as_abort(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".zshrc"
    rc.write_text("# untouched\n")
    _stub_dialog(monkeypatch, None)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh"])

    assert result.exit_code == 1, result.output
    assert rc.read_text() == "# untouched\n"


def test_completion_install_zsh_already_wired_is_idempotent(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".zshrc"
    # Pre-seed with a sentinel block; second install must replace its
    # body in place rather than appending a second copy.
    rc.write_text("# user content\n" + _wrap_sentinel("stale body\n"))
    _stub_dialog(monkeypatch, CompletionChoice.YES_AND_WIRE)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh"])

    assert result.exit_code == 0, result.output
    text = rc.read_text()
    assert text.count("# >>> setforge completion >>>") == 1
    assert "stale body" not in text
    assert "fpath=" in text


def test_completion_install_zsh_non_tty_without_flag_raises_mutate_gate(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".zshrc"
    rc.write_text("# untouched\n")
    monkeypatch.setattr("setforge.cli.completion._stdin_is_tty", lambda: False)
    # No dialog stub: if the code path reaches the dialog, the test will
    # fail with AttributeError on the lazy __getattr__ — but isatty=False
    # should short-circuit to the raise before the dialog import.

    result = _RUNNER.invoke(app, ["completion", "install", "zsh"])

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, ConfirmRequiresInteractive), result.exception


def test_completion_install_zsh_non_interactive_writes_and_wires(
    home: Path,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".zshrc"
    rc.write_text("# user content\n")
    result = _RUNNER.invoke(app, ["completion", "install", "zsh", "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert (home / ".config/setforge/completions/_setforge").exists()
    assert "fpath=" in rc.read_text()


def test_completion_install_zsh_non_interactive_no_wire_skips_rc(
    home: Path,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".zshrc"
    rc.write_text("# untouched\n")
    result = _RUNNER.invoke(
        app, ["completion", "install", "zsh", "--non-interactive", "--no-wire"]
    )

    assert result.exit_code == 0, result.output
    assert (home / ".config/setforge/completions/_setforge").exists()
    assert rc.read_text() == "# untouched\n"


def test_completion_install_zsh_refuses_when_rc_missing(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_show_completion: list[list[str]],
) -> None:
    # No ~/.zshrc on disk.
    _stub_dialog(monkeypatch, CompletionChoice.YES_AND_WIRE)
    result = _RUNNER.invoke(app, ["completion", "install", "zsh"])

    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SetforgeError)
    assert "rc file not found" in str(result.exception)


def test_completion_install_zsh_rc_file_override(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_show_completion: list[list[str]],
) -> None:
    custom_rc = home / "custom.zshrc"
    custom_rc.write_text("# custom rc\n")
    default_rc = home / ".zshrc"
    default_rc.write_text("# default untouched\n")
    _stub_dialog(monkeypatch, CompletionChoice.YES_AND_WIRE)

    result = _RUNNER.invoke(
        app,
        ["completion", "install", "zsh", "--rc-file", str(custom_rc)],
    )

    assert result.exit_code == 0, result.output
    assert "fpath=" in custom_rc.read_text()
    assert default_rc.read_text() == "# default untouched\n"


# ---------------------------------------------------------------------------
# completion install: bash (mockup K)
# ---------------------------------------------------------------------------


def test_completion_install_bash_idempotent_source_line(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".bashrc"
    rc.write_text("# user content\n")
    _stub_dialog(monkeypatch, CompletionChoice.YES_AND_WIRE)

    first = _RUNNER.invoke(app, ["completion", "install", "bash"])
    assert first.exit_code == 0, first.output
    after_first = rc.read_text()
    assert "source " in after_first
    assert "setforge.bash" in after_first

    # Re-stub the dialog (the previous fixture had `run_calls == 1`).
    _stub_dialog(monkeypatch, CompletionChoice.YES_AND_WIRE)
    second = _RUNNER.invoke(app, ["completion", "install", "bash"])
    assert second.exit_code == 0, second.output
    assert rc.read_text() == after_first


def test_completion_install_bash_non_interactive_writes_files(
    home: Path,
    fake_show_completion: list[list[str]],
) -> None:
    rc = home / ".bashrc"
    rc.write_text("# user content\n")
    result = _RUNNER.invoke(app, ["completion", "install", "bash", "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert (home / ".config/setforge/completions/setforge.bash").exists()
    assert "setforge.bash" in rc.read_text()


# ---------------------------------------------------------------------------
# completion install: fish (mockup K)
# ---------------------------------------------------------------------------


def test_completion_install_fish_writes_to_fish_dir_no_rc_edit(
    home: Path,
    fake_show_completion: list[list[str]],
) -> None:
    # No bashrc / zshrc and no dialog stub — fish must skip both paths.
    result = _RUNNER.invoke(app, ["completion", "install", "fish"])

    assert result.exit_code == 0, result.output
    target = home / ".config/fish/completions/setforge.fish"
    assert target.exists()
    assert target.read_text() == _FAKE_FISH_SCRIPT


def test_completion_install_fish_idempotent_no_op_second_run(
    home: Path,
    fake_show_completion: list[list[str]],
) -> None:
    target = home / ".config/fish/completions/setforge.fish"
    first = _RUNNER.invoke(app, ["completion", "install", "fish"])
    assert first.exit_code == 0
    first_mtime_text = target.read_text()
    second = _RUNNER.invoke(app, ["completion", "install", "fish"])
    assert second.exit_code == 0
    assert target.read_text() == first_mtime_text


# ---------------------------------------------------------------------------
# generic CLI surface
# ---------------------------------------------------------------------------


def test_completion_install_unknown_shell_exits_2(home: Path) -> None:
    result = _RUNNER.invoke(app, ["completion", "install", "tcsh"])
    assert result.exit_code == 2, result.output


def test_completion_install_show_completion_failure_raises_setforge_error(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (home / ".zshrc").write_text("# x\n")

    def fail_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr("setforge.cli.completion.subprocess.run", fail_run)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh", "--non-interactive"])

    assert result.exit_code != 0
    assert isinstance(result.exception, SetforgeError)
    assert "boom" in str(result.exception)


def test_completion_install_show_completion_empty_output_raises(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (home / ".zshrc").write_text("# x\n")

    def empty_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="   \n", stderr="")

    monkeypatch.setattr("setforge.cli.completion.subprocess.run", empty_run)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh", "--non-interactive"])

    assert result.exit_code != 0
    assert isinstance(result.exception, SetforgeError)
    assert "empty output" in str(result.exception)


# ---------------------------------------------------------------------------
# script path resolution
# ---------------------------------------------------------------------------


def test_script_path_zsh(home: Path) -> None:
    expected = home / ".config/setforge/completions/_setforge"
    assert _script_path(ShellKind.ZSH) == expected


def test_script_path_bash(home: Path) -> None:
    assert _script_path(ShellKind.BASH) == (
        home / ".config/setforge/completions/setforge.bash"
    )


def test_script_path_fish(home: Path) -> None:
    assert _script_path(ShellKind.FISH) == (
        home / ".config/fish/completions/setforge.fish"
    )


def test_completion_module_lazy_radiolist_attr_resolves() -> None:
    """The PEP 562 __getattr__ exposes prompt_toolkit's radiolist_dialog."""
    dialog = completion_mod.radiolist_dialog
    assert callable(dialog)


def test_completion_module_lazy_unknown_attr_raises() -> None:
    with pytest.raises(AttributeError):
        completion_mod.does_not_exist  # noqa: B018 — attribute access has side effect
