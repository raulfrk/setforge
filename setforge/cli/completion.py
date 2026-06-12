"""``setforge completion install`` — write shell completion scripts.

Idempotency is enforced via a sentinel block
(``# >>> setforge completion >>>`` / ``# <<< setforge completion <<<``)
in zsh / bash rc files: re-running the command replaces the body
between the markers rather than appending a second copy.
"""

from __future__ import annotations

import importlib.resources
import logging
import os
import re
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any, assert_never

import typer
from rich.console import Console

from setforge.cli import app
from setforge.cli._help_examples import COMPLETION_INSTALL_EXAMPLES
from setforge.errors import ConfirmRequiresInteractive, SetforgeError

LOGGER: logging.Logger = logging.getLogger(__name__)

# Bound on the ``setforge --show-completion=<shell>`` child subprocess.
# Anything past this is treated as a hard fault and falls back to the
# vendored template — typer's completion generation is sub-second in
# practice, so a 10s wait is generous slack for a healthy install.
_SHOW_COMPLETION_TIMEOUT_SECONDS = 10.0

# ``prompt_toolkit.shortcuts.radiolist_dialog`` resolves through this
# module's PEP 562 ``__getattr__`` so cold-start commands (``setforge
# --help``, ``setforge validate``) skip the ~140ms prompt_toolkit
# import.


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
    rich_markup_mode=None,
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


def _vendored_template_name(shell: ShellKind) -> str:
    """Return the package-data filename for ``shell``'s vendored template."""
    if shell is ShellKind.ZSH:
        return "_setforge"
    if shell is ShellKind.BASH:
        return "setforge.bash"
    if shell is ShellKind.FISH:
        return "setforge.fish"
    assert_never(shell)


def _load_vendored_template(shell: ShellKind) -> str:
    """Load the vendored fallback completion script for ``shell``.

    Reads the file shipped as package data under
    :mod:`setforge.cli.completions`. Used only on the fallback arm of
    :func:`_render_completion_script` — callers must already have logged
    WHY they're falling back before invoking this.
    """
    name = _vendored_template_name(shell)
    return (
        importlib.resources.files("setforge.cli.completions")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def _render_completion_script(shell: ShellKind) -> str:
    """Return the completion script for ``shell``, preferring typer-generated.

    Tries ``setforge --show-completion=<shell>`` in a subprocess; on any
    of the four documented failure modes (binary missing, subprocess
    timeout, non-zero exit, empty stdout) logs a WARNING that names the
    failure mode and falls back to the vendored template shipped under
    :mod:`setforge.cli.completions`. The vendored copy is seeded from
    typer-generated output at commit time, so the fallback content is
    drop-in compatible with the wiring lines :func:`_write_wiring`
    appends to the user's rc file.
    """
    # Without _TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION=1, typer
    # treats --show-completion as a bool and falls back to the parent
    # $SHELL — wrong when we're installing for a DIFFERENT shell.
    child_env = {**os.environ, "_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION": "1"}
    # ``shutil.which`` resolves ``setforge`` on PATH the same way the
    # user's shell did when they invoked us; falling back to
    # ``sys.argv[0]`` lets the command still work when called via an
    # absolute path that isn't on PATH (e.g. ``uv run setforge ...``
    # inside a venv whose bin dir wasn't activated).
    bin_path = shutil.which("setforge") or sys.argv[0]
    try:
        result = subprocess.run(
            [bin_path, f"--show-completion={shell.value}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SHOW_COMPLETION_TIMEOUT_SECONDS,
            env=child_env,
        )
    except FileNotFoundError:
        LOGGER.warning(
            "setforge --show-completion %s fallback: binary not found at %r "
            "(FileNotFoundError); using vendored template",
            shell.value,
            bin_path,
        )
        return _load_vendored_template(shell)
    except subprocess.TimeoutExpired:
        LOGGER.warning(
            "setforge --show-completion %s fallback: subprocess timeout after "
            "%.1fs; using vendored template",
            shell.value,
            _SHOW_COMPLETION_TIMEOUT_SECONDS,
        )
        return _load_vendored_template(shell)
    if result.returncode != 0:
        LOGGER.warning(
            "setforge --show-completion %s fallback: typer regression "
            "(exit %d) stderr=%r; using vendored template",
            shell.value,
            result.returncode,
            result.stderr.strip(),
        )
        return _load_vendored_template(shell)
    if not result.stdout.strip():
        LOGGER.warning(
            "setforge --show-completion %s fallback: empty stdout from "
            "subprocess (exit %d); using vendored template",
            shell.value,
            result.returncode,
        )
        return _load_vendored_template(shell)
    return result.stdout


def _zsh_wiring_body() -> str:
    """Return the zsh wiring lines that go inside the sentinel block.

    Uses ``$HOME`` (not literal ``~``) so the line works in any zsh
    context — ``~`` only expands at the start of an unquoted word.
    Guards the ``compinit`` call with an ``if`` so the snippet is a
    safe-no-op when the user has already configured compinit upstream.
    """
    # Deviation from mockup K: $HOME-quoted fpath + compinit guard
    # prevent dirty-zshrc regression when the user already wires
    # compinit themselves.
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


def _atomic_write_rc_file(rc_path: Path, content: str) -> None:
    """Atomically replace ``rc_path``'s content with ``content``.

    Writes to ``<rc_path>.setforge-tmp`` in the same directory, mirrors
    the existing file's mode bits via :func:`shutil.copystat`, then
    ``os.replace`` swaps the tmp file over the target. The
    same-directory placement is load-bearing — ``os.replace`` is only
    atomic when source and destination live on the same filesystem,
    which the parent-dir placement guarantees. The caller has already
    validated that ``rc_path`` exists.
    """
    tmp = rc_path.parent / f"{rc_path.name}.setforge-tmp"
    tmp.write_text(content, encoding="utf-8")
    # Copy mode bits (+ atime/mtime + flags where supported) from the
    # original BEFORE the replace so the swapped-in file inherits the
    # user's chmod choices (e.g. 0600 on a private rc file).
    shutil.copystat(rc_path, tmp)
    os.replace(tmp, rc_path)


def _write_wiring(rc_path: Path, body: str) -> None:
    """Insert/replace the setforge sentinel block in ``rc_path``.

    If the block exists, the body between markers is replaced verbatim
    (so a future setforge release that changes the wiring lines lands
    on a re-install without leaving a stale copy). Otherwise the block
    is appended to the end of the file. Refuses to create ``rc_path``
    if it doesn't exist — the user's shell-rc file is their territory.

    The actual disk write goes through :func:`_atomic_write_rc_file` so
    a SIGINT mid-write leaves the original rc file byte-identical (the
    tmp file is the only victim).
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
    _atomic_write_rc_file(rc_path, new_text)


def _stdin_is_tty() -> bool:
    """Return True iff stdin is connected to a terminal."""
    # Indirected so tests monkeypatch this attribute directly — Typer's
    # CliRunner swaps sys.stdin for a non-TTY stream and a naive
    # monkeypatch on the original sys.stdin doesn't survive.
    return sys.stdin.isatty()


def _resolve_non_dialog_choice(
    *,
    shell: ShellKind,
    non_interactive: bool,
    no_wire: bool,
) -> CompletionChoice | None:
    """Resolve the install choice WITHOUT showing a dialog, or ``None``.

    Two non-dialog paths short-circuit the prompt:
    ``--non-interactive`` picks ``YES_ONLY`` when ``no_wire`` else
    ``YES_AND_WIRE``; non-TTY stdin without ``--non-interactive``
    raises :class:`ConfirmRequiresInteractive` per the mutate-gate
    pattern in :func:`setforge.cli._confirm.confirm_auto_operation`.
    Returns ``None`` when the caller still needs to run the dialog.
    """
    if non_interactive:
        return CompletionChoice.YES_ONLY if no_wire else CompletionChoice.YES_AND_WIRE
    if not _stdin_is_tty():
        raise ConfirmRequiresInteractive(
            f"setforge completion install {shell.value} requires --non-interactive "
            "when stdin is not a TTY"
        )
    return None


def _run_install_dialog(shell: ShellKind) -> CompletionChoice:
    """Show the arrow-key install-confirm dialog and return the choice.

    ESC / Ctrl-C on the dialog is reported by prompt_toolkit as a
    ``None`` result; we treat that as :attr:`CompletionChoice.ABORT`.
    """
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


def _prompt_install_choice(
    *,
    shell: ShellKind,
    non_interactive: bool,
    no_wire: bool,
) -> CompletionChoice:
    """Resolve the install-confirm prompt → :class:`CompletionChoice`.

    Delegates non-dialog short-circuits to
    :func:`_resolve_non_dialog_choice` and falls through to
    :func:`_run_install_dialog` when interactive input is required.
    """
    resolved = _resolve_non_dialog_choice(
        shell=shell, non_interactive=non_interactive, no_wire=no_wire
    )
    if resolved is not None:
        return resolved
    return _run_install_dialog(shell)


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


def _install_fish(script_path: Path, content: str, console: Console) -> None:
    """Fish branch of ``completion install``: write script + print test line.

    Fish auto-loads from ``~/.config/fish/completions/``, so no rc edit
    is required — just drop the script and tell the user to open a new
    fish shell.
    """
    wrote = _write_script(script_path, content)
    verb = "wrote" if wrote else "unchanged"
    console.print(f"[green]✓[/green] {verb} {script_path}")
    console.print("=== install complete ===")
    console.print(_post_install_test_line(ShellKind.FISH))


def _install_zsh_or_bash(
    shell: ShellKind,
    rc: Path | None,
    content: str,
    script_path: Path,
    *,
    non_interactive: bool,
    no_wire: bool,
    console: Console,
) -> None:
    """Zsh/bash branch of ``completion install``: confirm + script + wiring.

    Resolves the install-confirm choice (dialog or non-interactive
    short-circuit), writes the completion script when the user did not
    abort, and rewrites the sentinel-bracketed wiring block in ``rc``
    unless the user picked ``YES_ONLY``. Banner strings ("=== <shell>
    completion install ===", "detected completion location:",
    "checking fpath wiring...", "=== this install will ===") mirror
    mockup K's verbatim text so the install flow reads as the spec
    described.
    """
    console.print(f"=== {shell.value} completion install ===")
    console.print(f"detected completion location: {script_path}")
    if shell is ShellKind.ZSH and rc is not None:
        console.print(f"checking fpath wiring... ({rc})")
    console.print("=== this install will ===")
    console.print(f"  - write completion script to {script_path}")
    if not no_wire and rc is not None:
        console.print(f"  - append sentinel-wrapped wiring block to {rc}")
    console.print("=== confirm ===")

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

    if rc is None:  # zsh + bash always resolve an rc path; survive python -O
        raise SetforgeError(f"internal: no rc path resolved for shell {shell.value!r}")
    already_wired = _detect_wiring(rc)
    body = _wiring_body_for(shell)
    _write_wiring(rc, body)
    rc_verb = "already wired (no-op)" if already_wired else f"appended block to {rc}"
    console.print(f"[green]✓[/green] {rc_verb}")
    console.print("=== install complete ===")
    console.print(_post_install_test_line(shell))


@completion_app.command("install", epilog=COMPLETION_INSTALL_EXAMPLES)
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
    console = Console(stderr=True)
    script_path = _script_path(shell)
    rc = rc_file.expanduser() if rc_file is not None else _rc_path(shell)
    content = _render_completion_script(shell)

    match shell:
        case ShellKind.FISH:
            _install_fish(script_path, content, console)
        case ShellKind.ZSH | ShellKind.BASH:
            _install_zsh_or_bash(
                shell,
                rc,
                content,
                script_path,
                non_interactive=non_interactive,
                no_wire=no_wire,
                console=console,
            )
        case _:
            assert_never(shell)
