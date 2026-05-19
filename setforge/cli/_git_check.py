"""Pre-deploy git-status check helpers for ``setforge install``.

Runs two narrow checks BEFORE the drift-gate flow on every ``install``
that does not pass ``--no-git-check``:

1. **Path source — uncommitted changes**: ``git status --porcelain=v2
   --ignore-submodules=all`` against the source directory. Any output
   lines mean the working tree is dirty; surface them to the user via
   the 3-option choice prompt (abort default / proceed / show-diff).
2. **Git source — cache behind remote**: ``git ls-remote origin main``
   against the cached clone vs the cache's local HEAD; on lag, list
   the missing commits via ``git log --oneline LOCAL..REMOTE``.

Both checks tolerate edge cases without blocking install: bare repos
log-and-proceed silently (rare; nothing to compare); ``ls-remote``
network failure warns and proceeds (transient failures must not block
a deploy); detached HEAD surfaces a warning and still presents the
3-option prompt.

The choice prompt is a **mutate-gate** per the project's
``feedback_mutate_gate_vs_failure_prompt`` memory: non-TTY without
``--no-git-check`` AND a dirty/stale tree RAISES
:class:`ConfirmRequiresInteractive`. Install IS about to mutate live,
so consent must be explicit — no silent fallback to "proceed".
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from setforge.errors import ConfirmRequiresInteractive
from setforge.source import PathSource, Source, resolve_source_dir

LOGGER: logging.Logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS: int = 30
_GIT_LOCALE_ENV: dict[str, str] = {"LANG": "C", "LC_ALL": "C"}

# prompt_toolkit's ``radiolist_dialog`` resolves through this module's
# lazy ``__getattr__`` below — mirrors :mod:`setforge.cli._confirm` and
# :mod:`setforge.cli.init` so cold-start commands (``setforge --help``)
# never pay the ~140ms prompt_toolkit import. Tests monkeypatch
# ``setforge.cli._git_check.radiolist_dialog`` through this same path.


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GitCheckChoice",
    "check_git_source_fresh",
    "check_path_source_clean",
    "prompt_git_check_choice",
    "run_git_check_or_raise",
]


class GitCheckChoice(StrEnum):
    """User's choice from the 3-option pre-deploy git-status prompt."""

    ABORT = "abort"
    PROCEED = "proceed"
    SHOW_DIFF = "show-diff"


def _git_run(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int = _GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a ``git`` subcommand with locale lockdown + capture_output.

    Locale is pinned to ``C`` on every invocation because non-porcelain
    output (``rev-parse``, ``log``, ``ls-remote``) is locale-sensitive.
    ``check=False`` so callers inspect ``returncode`` and ``stderr``
    explicitly — git's exit code carries signal (e.g. ``128`` for
    "not a git repository") that must not be masked by an exception.
    """
    env = {**os.environ, **_GIT_LOCALE_ENV}
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _is_bare_repository(source_dir: Path) -> bool:
    """Return True if ``source_dir`` is a bare git repository.

    ``git status`` errors on bare repos; the guard lets callers skip
    the porcelain check and proceed silently. Returns False on any
    error reading the rev-parse flag (e.g. not a git repo at all —
    handled by the caller via the porcelain-error path).
    """
    result = _git_run(
        ["git", "-C", str(source_dir), "rev-parse", "--is-bare-repository"],
        cwd=source_dir,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _is_detached_head(source_dir: Path) -> bool:
    """Return True if ``source_dir``'s HEAD is detached.

    Parses ``git status --porcelain=v2 --branch`` for the
    ``branch.head`` header; the value is literally ``(detached)`` when
    HEAD is not on a branch. Locale-stable because ``--porcelain=v2``
    output is normalized.
    """
    result = _git_run(
        [
            "git",
            "-C",
            str(source_dir),
            "status",
            "--porcelain=v2",
            "--branch",
            "--ignore-submodules=all",
        ],
        cwd=source_dir,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if line.startswith("# branch.head "):
            return line.split(" ", 2)[2].strip() == "(detached)"
    return False


def check_path_source_clean(source_dir: Path) -> list[str]:
    """Return non-clean lines from ``git status --porcelain=v2`` for ``source_dir``.

    Empty list means the working tree is clean (or the repo is bare —
    bare repos have no working tree to dirty, so they are treated as
    clean for install's purposes). Non-empty list means there are
    uncommitted changes; each entry is a one-line summary derived
    from the v2 porcelain entry (status code + path).

    Submodules are ignored (``--ignore-submodules=all``) so nested
    untracked entries don't surface as ``??`` noise.

    On a non-git directory: log-and-return empty (the install proceeds
    because we cannot warn meaningfully without a git history to
    compare against — a path source need not be under version control).
    """
    if not (source_dir / ".git").exists():
        LOGGER.debug(
            "path source %s is not a git repo; skipping clean check", source_dir
        )
        return []
    if _is_bare_repository(source_dir):
        LOGGER.info(
            "path source %s is a bare git repo; skipping clean check", source_dir
        )
        return []
    result = _git_run(
        [
            "git",
            "-C",
            str(source_dir),
            "status",
            "--porcelain=v2",
            "--ignore-submodules=all",
        ],
        cwd=source_dir,
    )
    if result.returncode != 0:
        LOGGER.warning(
            "git status failed on %s (exit %d): %s",
            source_dir,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return []
    return [
        _format_porcelain_v2_line(line) for line in result.stdout.splitlines() if line
    ]


def _format_porcelain_v2_line(line: str) -> str:
    """Render one ``--porcelain=v2`` entry as a short ``<status> <path>`` line.

    v2 entry shapes (selected — see ``man git-status``):
    - ``1 <XY> N... <path>`` — ordinary tracked-file change
    - ``2 <XY> N... <orig> <path>`` — renamed/copied
    - ``u <XY> N... <path>`` — unmerged
    - ``? <path>`` — untracked
    - ``! <path>`` — ignored (we don't ask for ``--ignored`` so this
      should never appear, but handled defensively)

    Returns the raw line for any shape this helper doesn't decode so
    no information is dropped on novel git outputs.
    """
    parts = line.split(" ", 2)
    if not parts:
        return line
    tag = parts[0]
    if tag == "?":
        return f"?? {parts[1]}" if len(parts) >= 2 else line
    if tag == "!":
        return f"!! {parts[1]}" if len(parts) >= 2 else line
    if tag in {"1", "2", "u"} and len(parts) >= 3:
        # parts[1] is the XY field (2 chars); parts[2] is the rest of the
        # entry. The path is the LAST whitespace-separated token in
        # parts[2]; on rename/copy entries the new path follows the
        # original name (also last).
        xy = parts[1]
        rest = parts[2]
        path = rest.rsplit(" ", 1)[-1] if " " in rest else rest
        return f"{xy} {path}"
    return line


def check_git_source_fresh(cache_dir: Path) -> tuple[str, ...]:
    """Return the shortlog of commits the local cache lags behind remote by.

    Empty tuple if the cache is up-to-date OR ``git ls-remote`` failed
    (network / timeout — warn-and-proceed so a transient remote outage
    does not block install). Subsequent local ``git rev-parse`` /
    ``git log`` failures propagate as
    :class:`subprocess.CalledProcessError` or
    :class:`subprocess.TimeoutExpired` — those are local-only and
    should not fail under normal conditions; surfacing them as
    exceptions is intentional. Also returns an empty tuple when
    ``cache_dir`` is not a git repo (defensive log-and-skip).
    """
    if not (cache_dir / ".git").exists():
        LOGGER.debug(
            "git source cache %s is not a git repo; skipping fresh check", cache_dir
        )
        return ()
    ref = _resolve_default_branch(cache_dir)
    if ref is None:
        LOGGER.warning(
            "could not resolve default branch for %s; skipping freshness check",
            cache_dir,
        )
        return ()
    remote_sha = _ls_remote_sha(cache_dir, ref)
    if remote_sha is None:
        return ()
    local_result = _git_run(
        ["git", "-C", str(cache_dir), "rev-parse", ref],
        cwd=cache_dir,
    )
    if local_result.returncode != 0:
        LOGGER.warning(
            "git rev-parse %s failed on cache %s: %s",
            ref,
            cache_dir,
            (local_result.stderr or "").strip(),
        )
        return ()
    local_sha = local_result.stdout.strip()
    if local_sha == remote_sha:
        return ()
    log_result = _git_run(
        [
            "git",
            "-C",
            str(cache_dir),
            "log",
            "--oneline",
            f"{local_sha}..{remote_sha}",
        ],
        cwd=cache_dir,
    )
    if log_result.returncode != 0:
        # Remote SHA may not be present locally yet (no fetch since the
        # branch advanced) — surface a generic message instead of an
        # empty range.
        return (f"cache is behind origin/{ref} (remote {remote_sha[:7]} not fetched)",)
    return tuple(line for line in log_result.stdout.splitlines() if line)


def _resolve_default_branch(cache_dir: Path) -> str | None:
    """Resolve the cache's default branch via the ``origin`` remote HEAD."""
    result = _git_run(
        [
            "git",
            "-C",
            str(cache_dir),
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ],
        cwd=cache_dir,
    )
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    # symbolic-ref outputs "origin/<branch>" — strip the remote prefix.
    return head.removeprefix("origin/") or None


def _ls_remote_sha(cache_dir: Path, ref: str) -> str | None:
    """Return the SHA the remote points at for ``ref``; None on network failure."""
    try:
        result = _git_run(
            ["git", "-C", str(cache_dir), "ls-remote", "origin", ref],
            cwd=cache_dir,
        )
    except subprocess.TimeoutExpired:
        typer.secho(
            f"warning: git ls-remote timed out on {cache_dir} — "
            f"cannot check freshness, proceeding",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return None
    if result.returncode != 0:
        typer.secho(
            f"warning: git ls-remote failed on {cache_dir} — "
            f"cannot check freshness, proceeding",
            err=True,
            fg=typer.colors.YELLOW,
        )
        return None
    # ls-remote output: ``<sha>\t<ref>`` per line; take the first row.
    first = result.stdout.split("\n", 1)[0].strip()
    if not first:
        return None
    return first.split("\t", 1)[0]


def prompt_git_check_choice(
    *,
    source: Source,
    dirty_lines: list[str],
    detached: bool,
    console: Console | None = None,
) -> GitCheckChoice:
    """Render the 3-option pre-deploy choice prompt; return the user's choice.

    Mutate-gate semantics (per
    ``feedback_mutate_gate_vs_failure_prompt`` memory): RAISES
    :class:`ConfirmRequiresInteractive` when stdin is not a TTY, since
    install is about to mutate live state and the user MUST consent
    explicitly. ``--no-git-check`` is the automation escape hatch.

    Returns :data:`GitCheckChoice.ABORT` on Esc/None (consistent with
    :func:`setforge.cli._confirm.confirm_auto_operation`'s Esc-as-abort
    handling). Caller inspects the return value to choose between
    abort (exit 1), proceed (continue to drift gate), or show-diff
    (render the diff/log then re-prompt).
    """
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            "setforge install detected uncommitted/stale state in the config "
            "source and stdin is not a TTY; pass --no-git-check to skip this "
            "check (e.g. for CI / cron use)."
        )
    if console is None:
        console = Console()
    _render_panel(
        source=source,
        dirty_lines=dirty_lines,
        detached=detached,
        console=console,
    )
    # ``radiolist_dialog`` resolves through the module-level ``__getattr__``
    # lazy import; tests monkeypatch the same attribute path.
    from setforge.cli import _git_check as _self  # local alias for monkeypatch

    abort_label, proceed_label, show_label = _choice_labels(source=source)
    choice = _self.radiolist_dialog(
        title="setforge install — pre-deploy git check",
        text="The config source is not in a clean baseline. What do you want to do?",
        values=[
            (GitCheckChoice.ABORT, abort_label),
            (GitCheckChoice.PROCEED, proceed_label),
            (GitCheckChoice.SHOW_DIFF, show_label),
        ],
        default=GitCheckChoice.ABORT,
    ).run()
    if choice is None:
        return GitCheckChoice.ABORT
    return choice


def _choice_labels(*, source: Source) -> tuple[str, str, str]:
    """Per-source-kind labels for the 3-option choice prompt."""
    if isinstance(source, PathSource):
        return (
            "abort and let me commit first (default — safe)",
            "proceed anyway (deploy uncommitted state)",
            "show diff of uncommitted changes first",
        )
    return (
        "abort and let me fetch first (default — safe)",
        "proceed anyway (deploy stale state)",
        "show pending commits first",
    )


def _render_panel(
    *,
    source: Source,
    dirty_lines: list[str],
    detached: bool,
    console: Console,
) -> None:
    """Print the warning header + status lines + risks block to ``console``."""
    if isinstance(source, PathSource):
        header = (
            f"[yellow]warning:[/yellow] config repo "
            f"[cyan]{source.path.expanduser()}[/cyan] has uncommitted changes:"
        )
        deploy_msg = "this would deploy uncommitted state to the live host"
    else:
        header = (
            f"[yellow]warning:[/yellow] git source cache "
            f"[cyan]{resolve_source_dir(source)}[/cyan] is behind origin:"
        )
        deploy_msg = "this would deploy stale state to the live host"
    console.print(header)
    if detached:
        console.print(
            "[yellow]  config repo is in detached HEAD — "
            "review state before deploy[/yellow]"
        )
    for line in dirty_lines:
        console.print(f"  {line}")
    console.print(f"[bold red]=== {deploy_msg} ===[/bold red]")


def _render_show_diff(source: Source, *, console: Console) -> None:
    """Print the diff / pending-log for the source; both path and git kinds.

    Path source: ``git diff`` against HEAD (working-tree changes).
    Git source: ``git log --oneline LOCAL..REMOTE`` AND ``git diff
    LOCAL..REMOTE`` to show both commit titles and code changes.
    """
    source_dir = resolve_source_dir(source)
    if isinstance(source, PathSource):
        diff = _git_run(["git", "-C", str(source_dir), "diff"], cwd=source_dir)
        console.print(
            f"[bold]=== git diff of uncommitted changes ({source_dir}) ===[/bold]"
        )
        console.print(diff.stdout or "(no diff content)")
        return
    ref = _resolve_default_branch(source_dir) or "main"
    remote_sha = _ls_remote_sha(source_dir, ref)
    if remote_sha is None:
        console.print(
            "[yellow](cannot render diff — git ls-remote unavailable)[/yellow]"
        )
        return
    log = _git_run(
        ["git", "-C", str(source_dir), "log", "--oneline", f"HEAD..{remote_sha}"],
        cwd=source_dir,
    )
    diff = _git_run(
        ["git", "-C", str(source_dir), "diff", f"HEAD..{remote_sha}"],
        cwd=source_dir,
    )
    console.print(f"[bold]=== pending commits ({source_dir}) ===[/bold]")
    console.print(log.stdout or "(no commits)")
    console.print("[bold]=== pending diff ===[/bold]")
    console.print(diff.stdout or "(no diff content)")


def run_git_check_or_raise(
    *,
    source: Source,
    no_git_check: bool,
    console: Console | None = None,
) -> None:
    """Top-level entry point — runs the git check + dispatches the choice.

    Called from :func:`setforge.cli.install.install` BEFORE the drift
    gate. ``--no-git-check`` short-circuits to a no-op. On dirty / stale
    state, prompts the user via :func:`prompt_git_check_choice` and acts
    on the choice:

    - :data:`GitCheckChoice.ABORT` → raises :class:`typer.Exit(1)`.
    - :data:`GitCheckChoice.PROCEED` → returns None (install continues).
    - :data:`GitCheckChoice.SHOW_DIFF` → renders the diff / pending log,
      then re-prompts (loops until the user picks ABORT or PROCEED).
    """
    if no_git_check:
        return
    if console is None:
        console = Console()
    source_dir = resolve_source_dir(source)
    detached = False
    dirty_lines: list[str] = []
    if isinstance(source, PathSource):
        if (source_dir / ".git").exists() and not _is_bare_repository(source_dir):
            detached = _is_detached_head(source_dir)
        dirty_lines = check_path_source_clean(source_dir)
    else:
        # SourceKind.GIT — the cache_dir is the resolved on-disk clone.
        dirty_lines = list(check_git_source_fresh(source_dir))
    if not dirty_lines and not detached:
        return
    while True:
        choice = prompt_git_check_choice(
            source=source,
            dirty_lines=dirty_lines,
            detached=detached,
            console=console,
        )
        match choice:
            case GitCheckChoice.ABORT:
                console.print("[red]✗ aborted[/red] — no install performed")
                raise typer.Exit(code=1)
            case GitCheckChoice.PROCEED:
                console.print("[green]✓ proceeding[/green] with deploy")
                return
            case GitCheckChoice.SHOW_DIFF:
                _render_show_diff(source, console=console)
                # Loop back to re-prompt; the show-diff sub-action is
                # an inspection step, not a final answer.
