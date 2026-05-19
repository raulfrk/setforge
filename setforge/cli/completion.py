"""``setforge completion install`` — write shell completion scripts.

Mockup K (user-approved 2026-05-18). One subcommand,
``setforge completion install <shell>``, where ``<shell>`` is
``zsh``/``bash``/``fish``. Writes the typer-generated completion
script to ``~/.config/setforge/completions/`` and, for zsh + bash,
appends the wiring lines to the user's shell rc file behind an
arrow-key confirm (write+wire / write-only / abort). Fish auto-loads
its completions dir, so no rc edit is needed.

Idempotency is enforced via a sentinel block
(``# >>> setforge completion >>>`` / ``# <<< setforge completion <<<``)
in zsh / bash rc files: re-running the command replaces the body
between the markers rather than appending a second copy.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any, assert_never

import typer
from rich.console import Console

from setforge.cli import app
from setforge.errors import ConfirmRequiresInteractive, SetforgeError

# ``prompt_toolkit.shortcuts.radiolist_dialog`` resolves through this
# module's PEP 562 ``__getattr__`` so cold-start commands (``setforge
# --help``, ``setforge validate``) skip the ~140ms prompt_toolkit
# import. Tests monkeypatch ``setforge.cli.completion.radiolist_dialog``
# through the same attribute path; mirror :mod:`setforge.cli.init` and
# :mod:`setforge.cli._confirm` exactly.


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CompletionChoice",
    "ShellKind",
    "completion_app",
    "completion_install",
]


# Sentinel block markers wrapping the wiring lines we append to a user's
# shell rc file. The completion-install command rewrites the body
# between these markers on every invocation, so the file stays clean
# under repeated installs even when setforge later changes the snippet.
_SENTINEL_BEGIN = "# >>> setforge completion >>>"
_SENTINEL_END = "# <<< setforge completion <<<"
_SENTINEL_BLOCK_RE = re.compile(
    rf"\n?{re.escape(_SENTINEL_BEGIN)}.*?{re.escape(_SENTINEL_END)}\n?",
    re.DOTALL,
)


class ShellKind(StrEnum):
    """Closed set of shells ``setforge completion install`` supports."""

    ZSH = "zsh"
    BASH = "bash"
    FISH = "fish"


class CompletionChoice(StrEnum):
    """Outcome of the install-confirm arrow-key prompt (mockup K)."""

    YES_AND_WIRE = "yes-and-wire"
    YES_ONLY = "yes-only"
    ABORT = "abort"


completion_app: typer.Typer = typer.Typer(
    help="Install shell completion scripts.",
    no_args_is_help=True,
)
app.add_typer(completion_app, name="completion")


def _completion_dir() -> Path:
    """Canonical setforge completion-scripts directory."""
    return Path.home() / ".config/setforge/completions"


def _script_path(shell: ShellKind) -> Path:
    """Return the absolute target script path for ``shell``.

    Fish lives at ``~/.config/fish/completions/setforge.fish`` (fish
    auto-loads from there with no rc edit needed). Zsh and bash live
    under the canonical setforge completions dir.
    """
    if shell is ShellKind.FISH:
        return Path.home() / ".config/fish/completions/setforge.fish"
    if shell is ShellKind.ZSH:
        return _completion_dir() / "_setforge"
    if shell is ShellKind.BASH:
        return _completion_dir() / "setforge.bash"
    assert_never(shell)


def _rc_path(shell: ShellKind) -> Path | None:
    """Return the shell rc file we'd modify, or ``None`` for fish."""
    if shell is ShellKind.ZSH:
        return Path.home() / ".zshrc"
    if shell is ShellKind.BASH:
        return Path.home() / ".bashrc"
    if shell is ShellKind.FISH:
        return None
    assert_never(shell)


def _render_completion_script(shell: ShellKind) -> str:
    """Invoke ``setforge --show-completion=<shell>`` and capture stdout.

    Shells out to the same ``setforge`` binary the user invoked so the
    completion content stays in lock-step with whatever typer version
    is resolved at runtime. Sets
    ``_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION=1`` so typer accepts
    an explicit shell value via ``--show-completion=<shell>`` instead
    of auto-detecting from the parent ``SHELL`` env — without the
    override, ``--show-completion`` is a bool flag and discards any
    positional, defaulting to whatever the user's login shell happens
    to be (which is wrong when we're installing for a DIFFERENT shell).
    """
    child_env = {**os.environ, "_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION": "1"}
    result = subprocess.run(
        ["setforge", f"--show-completion={shell.value}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=child_env,
    )
    if result.returncode != 0:
        raise SetforgeError(
            f"setforge --show-completion {shell.value} failed "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    if not result.stdout.strip():
        raise SetforgeError(
            f"setforge --show-completion {shell.value} produced empty output"
        )
    return result.stdout


def _zsh_wiring_body() -> str:
    """Return the zsh wiring lines that go inside the sentinel block.

    Uses ``$HOME`` (not literal ``~``) so the line works in any zsh
    context — ``~`` only expands at the start of an unquoted word.
    Guards the ``compinit`` call with an ``if`` so the snippet is a
    safe-no-op when the user has already configured compinit upstream.
    """
    return (
        'fpath=("$HOME/.config/setforge/completions" $fpath)\n'
        "if ! command -v compinit >/dev/null 2>&1; then\n"
        "  autoload -U compinit && compinit\n"
        "fi\n"
    )


def _bash_wiring_body() -> str:
    """Return the bash wiring lines that go inside the sentinel block."""
    return 'source "$HOME/.config/setforge/completions/setforge.bash"\n'


def _wrap_sentinel(body: str) -> str:
    """Wrap ``body`` between the setforge sentinel markers + a leading newline."""
    return f"\n{_SENTINEL_BEGIN}\n{body}{_SENTINEL_END}\n"


def _detect_wiring(rc_path: Path) -> bool:
    """Return True iff the sentinel block already exists in ``rc_path``."""
    if not rc_path.exists():
        return False
    text = rc_path.read_text(encoding="utf-8")
    return _SENTINEL_BEGIN in text and _SENTINEL_END in text


def _write_wiring(rc_path: Path, body: str) -> None:
    """Insert/replace the setforge sentinel block in ``rc_path``.

    If the block exists, the body between markers is replaced verbatim
    (so a future setforge release that changes the wiring lines lands
    on a re-install without leaving a stale copy). Otherwise the block
    is appended to the end of the file. Refuses to create ``rc_path``
    if it doesn't exist — the user's shell-rc file is their territory.
    """
    if not rc_path.exists():
        raise SetforgeError(
            f"rc file not found at {rc_path} — create it first, then re-run"
        )
    existing = rc_path.read_text(encoding="utf-8")
    block = _wrap_sentinel(body)
    if _SENTINEL_BEGIN in existing:
        new_text = _SENTINEL_BLOCK_RE.sub(block, existing, count=1)
    else:
        # Ensure exactly one trailing newline on existing content so the
        # block lands on its own pair of lines.
        if existing and not existing.endswith("\n"):
            existing = f"{existing}\n"
        new_text = f"{existing}{block}"
    rc_path.write_text(new_text, encoding="utf-8")


def _stdin_is_tty() -> bool:
    """Return True iff stdin is connected to a terminal.

    Indirected through a module-level helper so tests can monkeypatch
    ``setforge.cli.completion._stdin_is_tty`` directly — Typer's
    ``CliRunner`` replaces ``sys.stdin`` with a non-TTY stream, so a
    naive ``monkeypatch.setattr('sys.stdin.isatty', ...)`` set on the
    original ``sys.stdin`` doesn't survive the runner's substitution.
    """
    return sys.stdin.isatty()


def _prompt_install_choice(
    *,
    shell: ShellKind,
    non_interactive: bool,
    no_wire: bool,
) -> CompletionChoice:
    """Resolve the install-confirm prompt → :class:`CompletionChoice`.

    Non-interactive precedence: ``--non-interactive`` skips the dialog
    and picks ``YES_ONLY`` (when ``no_wire`` is set) or
    ``YES_AND_WIRE`` (default). Non-TTY without ``--non-interactive``
    raises :class:`ConfirmRequiresInteractive` per the mutate-gate
    pattern in :func:`setforge.cli._confirm.confirm_auto_operation` —
    rc-file edits need explicit consent.
    """
    if non_interactive:
        return CompletionChoice.YES_ONLY if no_wire else CompletionChoice.YES_AND_WIRE
    if not _stdin_is_tty():
        raise ConfirmRequiresInteractive(
            f"setforge completion install {shell.value} requires --non-interactive "
            "when stdin is not a TTY"
        )
    from setforge.cli import completion as _self  # local alias for monkeypatch path

    result = _self.radiolist_dialog(
        title=f"setforge completion install {shell.value}",
        text="Pick how setforge should wire the completion script:",
        values=[
            (
                CompletionChoice.YES_AND_WIRE,
                "yes, write completion + wire shell rc (default)",
            ),
            (
                CompletionChoice.YES_ONLY,
                "yes, write completion only — I'll wire shell rc myself",
            ),
            (CompletionChoice.ABORT, "abort"),
        ],
        default=CompletionChoice.YES_AND_WIRE,
    ).run()
    if result is None:
        return CompletionChoice.ABORT
    assert isinstance(result, CompletionChoice)
    return result


def _write_script(script_path: Path, content: str) -> bool:
    """Write ``content`` to ``script_path``; return True if the file changed.

    Creates parent dirs as needed. Returns False when the existing file
    matches ``content`` byte-for-byte (re-install is a no-op for the
    script file itself).
    """
    if script_path.exists() and script_path.read_text(encoding="utf-8") == content:
        return False
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(content, encoding="utf-8")
    return True


def _wiring_body_for(shell: ShellKind) -> str:
    """Return the rc-file wiring body for ``shell`` (fish has none)."""
    if shell is ShellKind.ZSH:
        return _zsh_wiring_body()
    if shell is ShellKind.BASH:
        return _bash_wiring_body()
    if shell is ShellKind.FISH:
        return ""
    assert_never(shell)


def _post_install_test_line(shell: ShellKind) -> str:
    """Return the ``test:`` hint printed after a successful install."""
    if shell is ShellKind.ZSH:
        return (
            "  restart your shell or run:  exec zsh\n"
            "  test:  setforge install --profile=<TAB>"
        )
    if shell is ShellKind.BASH:
        return (
            "  restart your shell or run:  exec bash\n"
            "  test:  setforge install --profile=<TAB>"
        )
    if shell is ShellKind.FISH:
        return (
            "  fish auto-loads — open a new fish shell to pick up the script\n"
            "  test:  setforge install --profile=<TAB>"
        )
    assert_never(shell)


@completion_app.command("install")
def completion_install(
    shell: ShellKind = typer.Argument(
        ...,
        help="Shell to install completions for.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Skip the arrow-key confirm; required for non-TTY stdin.",
    ),
    no_wire: bool = typer.Option(
        False,
        "--no-wire",
        help="Write the completion script only; do not edit the shell rc file.",
    ),
    rc_file: Path | None = typer.Option(
        None,
        "--rc-file",
        help="Override the shell rc file path (default: ~/.zshrc or ~/.bashrc).",
    ),
) -> None:
    """Install shell completion for ``shell`` (zsh/bash/fish).

    Writes the typer-generated completion script to
    ``~/.config/setforge/completions/`` (or the fish auto-load dir),
    then optionally appends a sentinel-bracketed wiring block to the
    user's shell rc file behind an arrow-key confirm.
    """
    console = Console()
    script_path = _script_path(shell)
    rc = rc_file if rc_file is not None else _rc_path(shell)
    content = _render_completion_script(shell)

    # Fish auto-loads: write the script, print the test line, done.
    if shell is ShellKind.FISH:
        wrote = _write_script(script_path, content)
        verb = "wrote" if wrote else "unchanged"
        console.print(f"[green]✓[/green] {verb} {script_path}")
        console.print("=== install complete ===")
        console.print(_post_install_test_line(shell))
        return

    # zsh / bash share the confirm + sentinel-block path.
    choice = _prompt_install_choice(
        shell=shell,
        non_interactive=non_interactive,
        no_wire=no_wire,
    )
    if choice is CompletionChoice.ABORT:
        console.print("[red]✗ aborted[/red] — no changes made")
        raise typer.Exit(code=1)

    wrote_script = _write_script(script_path, content)
    script_verb = "wrote" if wrote_script else "unchanged"
    console.print(f"[green]✓[/green] {script_verb} {script_path}")

    if choice is CompletionChoice.YES_ONLY:
        console.print(f"[yellow]ⓘ[/yellow] skipped rc-file edit; wire {rc} yourself")
        console.print("=== install complete ===")
        console.print(_post_install_test_line(shell))
        return

    assert rc is not None  # zsh + bash always have an rc path
    already_wired = _detect_wiring(rc)
    body = _wiring_body_for(shell)
    _write_wiring(rc, body)
    rc_verb = "already wired (no-op)" if already_wired else f"appended block to {rc}"
    console.print(f"[green]✓[/green] {rc_verb}")
    console.print("=== install complete ===")
    console.print(_post_install_test_line(shell))
