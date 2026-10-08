"""``setforge upgrade`` — PyPI version check + uv wrapper.

Single-command surface that fetches the latest setforge release from
PyPI and shells out to ``uv tool upgrade setforge`` after an arrow-key
confirm with three choices: abort, upgrade, upgrade + migrate-check
(the default). Release notes are not shown: the installed wheel does not
carry the changelog, so the command prints the changelog URL instead.

Flags:

* ``--check`` — read-only PyPI report; no mutation.
* ``--no-prompt`` — skip the confirm for automation; runs the default
  ``upgrade-and-migrate-check`` choice.
* ``--to=X.Y.Z`` — pin the target version (bypasses PyPI selection;
  PyPI is still hit for the version's yanked / prerelease status).
* ``--prerelease`` — include pre-release versions when picking latest.

Output of the success path includes the explicit rollback command
``uv tool install --reinstall --reinstall-package setforge==<prev>``
so the user can revert without leaving the terminal.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast

import typer
from packaging.version import InvalidVersion, Version
from rich.console import Console
from rich.panel import Panel

from setforge import __version__ as _CURRENT_VERSION
from setforge._pypi_client import (
    PyPIVersionInfo,
    fetch_latest_version,
    fetch_version_info,
)
from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import UPGRADE_EXAMPLES
from setforge.cli._output import make_console
from setforge.errors import (
    ConfirmRequiresInteractive,
    PyPIFetchError,
    SetforgeError,
    UpgradeError,
)
from setforge.locking import mutation_locks

__all__ = [
    "UpgradeChoice",
    "UpgradePlan",
]

_PACKAGE_NAME: str = "setforge"
_CHANGELOG_URL: str = "https://github.com/raulfrk/setforge/blob/main/CHANGELOG.md"


class UpgradeChoice(StrEnum):
    """Closed set of radiolist outcomes for the upgrade confirm panel."""

    ABORT = "abort"
    UPGRADE = "upgrade"
    UPGRADE_AND_MIGRATE_CHECK = "upgrade-and-migrate-check"


@dataclass(slots=True, frozen=True)
class UpgradePlan:
    """Fully-built input to the confirm panel + the wrap.

    Carries the version pair, a flag for major-version bumps, and the
    yanked / pre-release status PyPI reports for the target.
    """

    current_version: str
    target_version: str
    is_major_bump: bool
    yanked: bool = False
    yanked_reason: str | None = None
    is_prerelease: bool = False
    extra_warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def _is_major_bump(current: str, target: str) -> bool:
    """Return True when ``target`` is a strictly-greater major version."""
    try:
        return Version(target).major > Version(current).major
    except InvalidVersion:
        return False


def _canonical_version(to: str) -> str:
    """Return the canonical PEP 440 spelling of a --to pin, or raise.

    The previous hand-rolled X.Y.Z pattern rejected canonical prerelease
    spellings such as 2.0.0rc1 while accidentally accepting 2.0.0-rc1.
    Version is the same authority the rest of the resolution already uses, and
    canonicalising means the post-upgrade verification compares against the
    spelling uv itself reports.
    """
    try:
        return str(Version(to))
    except InvalidVersion as exc:
        raise UpgradeError(
            f"--to value {to!r} is not a valid version string: {exc}"
        ) from exc


def _build_upgrade_plan(*, to: str | None, prerelease: bool) -> UpgradePlan:
    """Resolve the target version and its PyPI status → UpgradePlan."""
    if to is not None:
        to = _canonical_version(to)
    info: PyPIVersionInfo = fetch_latest_version(
        package=_PACKAGE_NAME,
        current_version=_CURRENT_VERSION,
        include_prereleases=prerelease or (to is not None and _looks_prerelease(to)),
    )
    target_info = (
        fetch_version_info(
            package=_PACKAGE_NAME,
            version=to,
            current_version=_CURRENT_VERSION,
        )
        if to is not None
        else info
    )
    target = target_info.version
    warnings: list[str] = []
    if to is not None and to != info.version:
        warnings.append(
            f"--to={to} pins a version other than PyPI latest ({info.version})."
        )
    return UpgradePlan(
        current_version=_CURRENT_VERSION,
        target_version=target,
        is_major_bump=_is_major_bump(_CURRENT_VERSION, target),
        yanked=target_info.yanked,
        yanked_reason=target_info.yanked_reason,
        is_prerelease=target_info.is_prerelease,
        extra_warnings=tuple(warnings),
    )


def _looks_prerelease(version: str) -> bool:
    """Best-effort check: is ``version`` a pre-release per PEP 440?"""
    try:
        return Version(version).is_prerelease
    except InvalidVersion:
        return False


# ---------------------------------------------------------------------------
# Confirm panel + radiolist
# ---------------------------------------------------------------------------


def _render_confirm_panel(plan: UpgradePlan, *, console: Console) -> None:
    """Print the pre-confirm panel: header, PyPI warnings and the changelog URL."""
    header = (
        f"[bold]setforge upgrade[/bold] "
        f"[cyan]{plan.current_version}[/cyan] → "
        f"[yellow]{plan.target_version}[/yellow]"
    )
    console.print(Panel.fit(header, title="confirmation required"))

    if plan.yanked:
        reason = plan.yanked_reason or "no reason provided"
        console.print(
            f"[bold red]YANKED:[/bold red] target {plan.target_version} "
            f"is yanked on PyPI ({reason})."
        )
    if plan.is_prerelease:
        console.print(
            f"[yellow]PRE-RELEASE:[/yellow] {plan.target_version} is a pre-release."
        )
    if plan.is_major_bump:
        console.print(
            f"[bold yellow]MAJOR BUMP:[/bold yellow] "
            f"{plan.current_version} → {plan.target_version}"
        )
    for warning in plan.extra_warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")

    console.print(f"changelog: {_CHANGELOG_URL}")


def _confirm_upgrade(plan: UpgradePlan, *, yes: bool) -> UpgradeChoice:
    """Render the panel and prompt arrow-key choice; return the user's pick.

    ``yes=True`` (``--no-prompt``) auto-picks ``UPGRADE_AND_MIGRATE_CHECK``,
    the default. Esc / None from the dialog → ABORT.
    """
    default_choice = UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK
    if yes:
        return default_choice

    console = make_console()
    _render_confirm_panel(plan, console=console)

    from setforge.ui.widgets import CANCEL, Button, button_bar

    buttons = [
        Button("Abort — no changes", UpgradeChoice.ABORT),
        Button("Upgrade", UpgradeChoice.UPGRADE),
        Button(
            "Upgrade + run `setforge migrate --check`",
            UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK,
        ),
    ]
    initial = next(
        (i for i, button in enumerate(buttons) if button.value is default_choice), 0
    )
    choice = button_bar(
        buttons,
        title="setforge upgrade",
        body="Proceed?",
        initial=initial,
    )
    if choice is CANCEL:
        console.print("[red]✗ aborted[/red] (Esc) — no changes")
        return UpgradeChoice.ABORT
    if choice is UpgradeChoice.ABORT:
        console.print("[red]✗ aborted[/red] — no changes")
        return UpgradeChoice.ABORT
    return cast(UpgradeChoice, choice)


# ---------------------------------------------------------------------------
# uv tool upgrade wrapper
# ---------------------------------------------------------------------------


def _run_uv_tool_upgrade(*, target: str, pinned: bool) -> None:
    """Shell out to upgrade setforge; parse STDOUT, not exit code.

    When ``pinned`` (the user passed ``--to=<version>``) the install is
    pinned to the exact target via ``uv tool install --reinstall-package``
    — ``uv tool upgrade`` cannot target a version and would silently pull
    PyPI-latest. Otherwise ``uv tool upgrade setforge`` moves to latest.

    Per research brief §2: ``uv tool upgrade`` returns exit 0 even on
    the no-op case where setforge is already at the latest version. The
    reliable signal is STDOUT — match ``"Nothing to upgrade"`` or
    ``"already up to date"``. This no-op detection applies only to the
    unpinned path; ``install --reinstall-package`` always reinstalls and
    never reports a no-op (``_verify_post_upgrade`` confirms the result).
    """
    uv = shutil.which("uv")
    if uv is None:
        raise UpgradeError(
            "uv not found on PATH; install from https://docs.astral.sh/uv/"
        )
    if pinned:
        cmd = [
            uv,
            "tool",
            "install",
            "--reinstall-package",
            _PACKAGE_NAME,
            f"{_PACKAGE_NAME}=={target}",
        ]
    else:
        cmd = [uv, "tool", "upgrade", _PACKAGE_NAME]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpgradeError(f"uv tool {cmd[2]} timed out after 120 seconds") from exc
    except OSError as exc:
        raise UpgradeError(f"uv tool {cmd[2]} could not start: {exc}") from exc
    if result.returncode != 0:
        raise UpgradeError(
            f"uv tool {cmd[2]} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    stdout_lower = result.stdout.lower()
    if "nothing to upgrade" in stdout_lower or "already up to date" in stdout_lower:
        typer.echo(f"setforge is already up to date ({target}); no-op.")


def _verify_post_upgrade(*, expected: str) -> None:
    """Run ``uv tool list`` and assert setforge reports the expected version.

    Real ``uv tool list`` output prints the version with a leading ``v``
    (``setforge v1.3.2``) and lists the package's executables on ``- `` lines
    beneath, so that prefix is optional here. This matches
    ``provision.python._parse_tools``, which strips the same prefix.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise UpgradeError("uv vanished from PATH between upgrade and verify")
    try:
        result = subprocess.run(
            [uv, "tool", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpgradeError(
            "post-upgrade verification (`uv tool list`) timed out after 30 seconds"
        ) from exc
    except OSError as exc:
        raise UpgradeError(
            f"post-upgrade verification (`uv tool list`) could not start: {exc}"
        ) from exc
    if result.returncode != 0:
        raise UpgradeError(
            f"uv tool list failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    pattern = re.compile(rf"^{re.escape(_PACKAGE_NAME)}\s+v?(\S+)\s*$", re.MULTILINE)
    try:
        wanted = Version(expected)
    except InvalidVersion as exc:  # pragma: no cover - --to validation runs first
        raise UpgradeError(
            f"post-upgrade verification: {expected!r} is not a version"
        ) from exc
    for match in pattern.finditer(result.stdout):
        try:
            installed = Version(match.group(1))
        except InvalidVersion:
            continue
        if installed == wanted:
            return
    raise UpgradeError(
        f"post-upgrade verification: did not see "
        f"`{_PACKAGE_NAME} {expected}` in `uv tool list` output"
    )


def _run_migrate_check_subprocess(*, config: Path) -> None:
    """Best-effort ``uv run setforge migrate --check --config <path>``.

    ``migrate`` is registered by a sibling component; when
    it has not landed yet the subprocess exits non-zero with a "no such
    command" message. Soft-fail in that case — print a hint and return.

    The manifest is passed explicitly because the subprocess would otherwise
    inherit the caller's working directory and validate whichever project the
    user happened to be standing in, which proves nothing about the install
    that was just upgraded.
    """
    uv = shutil.which("uv")
    if uv is None:
        typer.echo("uv missing — skipping migrate --check.")
        return
    typer.echo(f"checking schema migrations against {config}")
    try:
        result = subprocess.run(
            [
                uv,
                "run",
                _PACKAGE_NAME,
                "migrate",
                "--check",
                "--config",
                str(config),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpgradeError(
            "`setforge migrate --check` timed out after 60 seconds"
        ) from exc
    except OSError as exc:
        raise UpgradeError(
            f"`setforge migrate --check` could not start: {exc}"
        ) from exc
    if result.returncode == 0:
        typer.echo(result.stdout)
        return
    stderr_lower = result.stderr.lower()
    if "no such command" in stderr_lower or "no such option" in stderr_lower:
        typer.echo(
            "note: `setforge migrate` is not available in the upgraded "
            "version; skipping migrate-check."
        )
        return
    typer.secho(
        f"warning: `setforge migrate --check` exited "
        f"{result.returncode}: {result.stderr.strip()}",
        err=True,
        fg=typer.colors.YELLOW,
    )


# ---------------------------------------------------------------------------
# Reports + completion
# ---------------------------------------------------------------------------


def _print_check_report(plan: UpgradePlan) -> None:
    """Print the read-only ``--check`` report; no mutation."""
    console = make_console()
    _render_confirm_panel(plan, console=console)
    if plan.target_version == plan.current_version:
        console.print(
            f"[green]setforge is already on the latest version "
            f"({plan.current_version}).[/green]"
        )
    else:
        console.print(
            f"[cyan]upgrade available:[/cyan] {plan.current_version} → "
            f"{plan.target_version}"
        )


def _print_completion_report(plan: UpgradePlan) -> None:
    """Print the success report; include the explicit rollback command."""
    typer.secho(
        f"✓ setforge upgraded to {plan.target_version}",
        fg=typer.colors.GREEN,
    )
    typer.echo(
        f"rollback: uv tool install --reinstall "
        f"--reinstall-package setforge setforge=={plan.current_version}"
    )
    typer.echo(f"changelog: {_CHANGELOG_URL}")


# ---------------------------------------------------------------------------
# Typer entry point
# ---------------------------------------------------------------------------


@app.command(epilog=UPGRADE_EXAMPLES)
def upgrade(
    check: bool = typer.Option(
        False,
        "--check",
        help="Read-only: report current vs latest; no mutation.",
    ),
    no_prompt: bool = typer.Option(
        False,
        "--no-prompt",
        help="Skip the confirm; upgrade and run `migrate --check`.",
    ),
    to: str | None = typer.Option(
        None,
        "--to",
        help="Target a specific X.Y.Z version (instead of PyPI latest).",
    ),
    prerelease: bool = typer.Option(
        False,
        "--prerelease",
        help="Include pre-release versions when picking the latest.",
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Upgrade setforge: PyPI check + uv wrapper (mockup U)."""
    try:
        plan = _build_upgrade_plan(to=to, prerelease=prerelease)
    except PyPIFetchError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    if check:
        _print_check_report(plan)
        return

    if plan.target_version == plan.current_version:
        typer.echo(
            f"setforge is already on the latest version ({plan.current_version})."
        )
        return

    if no_prompt and not sys.stdin.isatty():
        # Automation path: skip the panel render, take the default
        # choice, run the wrap. Tests cover both branches.
        choice = _confirm_upgrade(plan, yes=True)
    elif not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            "setforge upgrade requires --no-prompt when stdin is not a TTY"
        )
    else:
        choice = _confirm_upgrade(plan, yes=no_prompt)

    if choice is UpgradeChoice.ABORT:
        return

    with mutation_locks(resources=True):
        _run_uv_tool_upgrade(target=plan.target_version, pinned=to is not None)
        _verify_post_upgrade(expected=plan.target_version)

    if choice is UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK:
        try:
            manifest = _resolve_config_arg(config)
        except (SetforgeError, ValueError, OSError) as exc:
            # Best-effort: migrate --check is advisory, so an unresolvable
            # manifest skips it instead of failing an upgrade that succeeded.
            # ValidationError and UnicodeDecodeError from a broken host config
            # are ValueErrors, and an unreadable file is an OSError.
            typer.echo(f"note: skipping migrate --check: {exc}")
        else:
            _run_migrate_check_subprocess(config=manifest)

    _print_completion_report(plan)
