"""Unit tests for ``setforge completion install`` (mockup K)."""

from __future__ import annotations

import stat
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

# The scripts earlier releases installed for each shell (they came from
# ``setforge --show-completion=<shell>``). The in-process generator must
# keep producing exactly these bytes; a Typer upgrade that changes them
# shows up here and needs a deliberate look before the golden is updated.
_ZSH_SCRIPT = (
    "#compdef setforge\n"
    "\n"
    "_setforge_completion() {\n"
    '  eval $(env _TYPER_COMPLETE_ARGS="${words[1,$CURRENT]}" '
    "_SETFORGE_COMPLETE=complete_zsh setforge)\n"
    "}\n"
    "\n"
    "compdef _setforge_completion setforge\n"
)
_BASH_SCRIPT = (
    "_setforge_completion() {\n"
    "    local IFS=$'\n'\n"
    '    COMPREPLY=( $( env COMP_WORDS="${COMP_WORDS[*]}" \\\n'
    "                   COMP_CWORD=$COMP_CWORD \\\n"
    "                   _SETFORGE_COMPLETE=complete_bash $1 ) )\n"
    "    return 0\n"
    "}\n"
    "\n"
    "complete -o default -F _setforge_completion setforge\n"
)
_FISH_SCRIPT = (
    "complete --command setforge --no-files --arguments "
    '"(env _SETFORGE_COMPLETE=complete_fish _TYPER_COMPLETE_FISH_ACTION=get-args '
    '_TYPER_COMPLETE_ARGS=(commandline -cp) setforge)" '
    '--condition "env _SETFORGE_COMPLETE=complete_fish '
    "_TYPER_COMPLETE_FISH_ACTION=is-args "
    '_TYPER_COMPLETE_ARGS=(commandline -cp) setforge"\n'
)
_SCRIPT_BY_SHELL = {
    ShellKind.ZSH: _ZSH_SCRIPT,
    ShellKind.BASH: _BASH_SCRIPT,
    ShellKind.FISH: _FISH_SCRIPT,
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-point ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _stub_dialog(monkeypatch: pytest.MonkeyPatch, return_value: object) -> None:
    def fake_dialog(*args: Any, **kwargs: Any) -> object:
        del args, kwargs
        return return_value

    monkeypatch.setattr("setforge.cli.completion.button_bar", fake_dialog)
    monkeypatch.setattr("setforge.cli.completion._stdin_is_tty", lambda: True)


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


def test_write_wiring_keeps_symlinked_rc_and_updates_its_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "dotfiles" / "zshrc"
    target.parent.mkdir()
    target.write_text("# user content\n")
    rc = tmp_path / ".zshrc"
    rc.symlink_to(target)

    _write_wiring(rc, "fpath=(test)\n")

    assert rc.is_symlink()
    assert rc.resolve() == target
    assert "fpath=(test)" in target.read_text()


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
) -> None:
    rc = home / ".zshrc"
    rc.write_text("# user content\nalias ls=ls\n")
    _stub_dialog(monkeypatch, CompletionChoice.YES_AND_WIRE)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh"])

    assert result.exit_code == 0, result.output
    assert (home / ".config/setforge/completions/_setforge").read_text() == (
        _ZSH_SCRIPT
    )
    text = rc.read_text()
    assert "fpath=" in text
    assert "compinit" in text
    assert "# >>> setforge completion >>>" in text
    assert "# user content" in text


def test_completion_install_zsh_yes_only_skips_rc_edit(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
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
) -> None:
    from setforge.ui.widgets import CANCEL

    rc = home / ".zshrc"
    rc.write_text("# untouched\n")
    _stub_dialog(monkeypatch, CANCEL)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh"])

    assert result.exit_code == 1, result.output
    assert rc.read_text() == "# untouched\n"


def test_completion_install_zsh_already_wired_is_idempotent(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
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
) -> None:
    rc = home / ".zshrc"
    rc.write_text("# user content\n")
    result = _RUNNER.invoke(app, ["completion", "install", "zsh", "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert (home / ".config/setforge/completions/_setforge").exists()
    assert "fpath=" in rc.read_text()


def test_completion_install_zsh_non_interactive_no_wire_skips_rc(
    home: Path,
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
) -> None:
    rc = home / ".bashrc"
    rc.write_text("# user content\n")
    _stub_dialog(monkeypatch, CompletionChoice.YES_AND_WIRE)

    first = _RUNNER.invoke(app, ["completion", "install", "bash"])
    assert first.exit_code == 0, first.output
    after_first = rc.read_text()
    assert "source " in after_first
    assert "setforge.bash" in after_first

    _stub_dialog(monkeypatch, CompletionChoice.YES_AND_WIRE)
    second = _RUNNER.invoke(app, ["completion", "install", "bash"])
    assert second.exit_code == 0, second.output
    assert rc.read_text() == after_first


def test_completion_install_bash_non_interactive_writes_files(
    home: Path,
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
) -> None:
    # No bashrc / zshrc and no dialog stub — fish must skip both paths.
    result = _RUNNER.invoke(app, ["completion", "install", "fish"])

    assert result.exit_code == 0, result.output
    target = home / ".config/fish/completions/setforge.fish"
    assert target.exists()
    assert target.read_text() == _FISH_SCRIPT


def test_completion_install_fish_idempotent_no_op_second_run(
    home: Path,
) -> None:
    target = home / ".config/fish/completions/setforge.fish"
    first = _RUNNER.invoke(app, ["completion", "install", "fish"])
    assert first.exit_code == 0
    first_mtime_text = target.read_text()
    second = _RUNNER.invoke(app, ["completion", "install", "fish"])
    assert second.exit_code == 0
    assert target.read_text() == first_mtime_text


# ---------------------------------------------------------------------------
# generated script: one in-process generator for every shell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", list(ShellKind))
def test_completion_install_writes_the_script_earlier_releases_installed(
    home: Path, shell: ShellKind
) -> None:
    result = _RUNNER.invoke(
        app, ["completion", "install", shell.value, "--non-interactive", "--no-wire"]
    )

    assert result.exit_code == 0, result.output
    assert _script_path(shell).read_text() == _SCRIPT_BY_SHELL[shell]


@pytest.mark.parametrize("shell", list(ShellKind))
def test_completion_install_writes_what_show_completion_prints(
    home: Path, shell: ShellKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without this, Typer reads --show-completion as a bare flag for the
    # shell running the tests instead of taking the shell name as a value.
    monkeypatch.setenv("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION", "1")
    printed = _RUNNER.invoke(
        app, [f"--show-completion={shell.value}"], prog_name="setforge"
    )
    assert printed.exit_code == 0, printed.output

    result = _RUNNER.invoke(
        app, ["completion", "install", shell.value, "--non-interactive", "--no-wire"]
    )

    assert result.exit_code == 0, result.output
    assert _script_path(shell).read_text() == printed.output


@pytest.mark.parametrize("shell", list(ShellKind))
def test_completion_install_starts_no_child_process(
    home: Path, shell: ShellKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("completion install must not start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)

    result = _RUNNER.invoke(
        app, ["completion", "install", shell.value, "--non-interactive", "--no-wire"]
    )

    assert result.exit_code == 0, result.output
    assert _script_path(shell).read_text() == _SCRIPT_BY_SHELL[shell]


def test_installed_zsh_script_does_not_call_compinit(home: Path) -> None:
    """The rc wiring block is the only place compinit is invoked (guarded)."""
    result = _RUNNER.invoke(
        app, ["completion", "install", "zsh", "--non-interactive", "--no-wire"]
    )

    assert result.exit_code == 0, result.output
    for line in _script_path(ShellKind.ZSH).read_text().splitlines():
        stripped = line.strip()
        assert not stripped.startswith("compinit"), line
        assert "autoload" not in stripped or "compinit" not in stripped, line


# ---------------------------------------------------------------------------
# generic CLI surface
# ---------------------------------------------------------------------------


def test_completion_install_unknown_shell_exits_2(home: Path) -> None:
    result = _RUNNER.invoke(app, ["completion", "install", "tcsh"])
    assert result.exit_code == 2, result.output


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


def test_completion_module_lazy_button_bar_attr_resolves() -> None:
    from setforge.ui.widgets import button_bar

    resolved = completion_mod.button_bar
    assert resolved is button_bar


def test_completion_module_lazy_unknown_attr_raises() -> None:
    with pytest.raises(AttributeError):
        completion_mod.does_not_exist  # noqa: B018 — attribute access has side effect


# ---------------------------------------------------------------------------
# Atomic rc-file write: SIGINT mid-write leaves original untouched.
# ---------------------------------------------------------------------------


def test_completion_install_keeps_the_rc_file_mode(home: Path) -> None:
    rc = home / ".zshrc"
    rc.write_text("# original\n")
    rc.chmod(0o600)

    result = _RUNNER.invoke(app, ["completion", "install", "zsh", "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert "# >>> setforge completion >>>" in rc.read_text()
    assert stat.S_IMODE(rc.stat().st_mode) == 0o600


def test_write_wiring_leaves_no_temp_file(tmp_path: Path) -> None:
    rc = tmp_path / ".bashrc"
    rc.write_text("# original\n")

    _write_wiring(rc, "body\n")

    assert list(tmp_path.iterdir()) == [rc]


def test_write_wiring_interrupted_mid_write_leaves_rc_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGINT landing after the new bytes are staged must not touch the rc file."""
    rc = tmp_path / ".zshrc"
    original = "# user content\nexport FOO=1\nalias ls='ls --color=auto'\n"
    rc.write_text(original)

    def interrupted(fd: int) -> None:
        raise KeyboardInterrupt("simulated SIGINT mid-write")

    monkeypatch.setattr("setforge.atomicio.os.fsync", interrupted)

    with pytest.raises(KeyboardInterrupt):
        _write_wiring(rc, "body\n")

    assert rc.read_text() == original
    assert list(tmp_path.iterdir()) == [rc]
