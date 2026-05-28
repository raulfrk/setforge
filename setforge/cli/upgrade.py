"""``setforge upgrade`` — PyPI version check + CHANGELOG notes + uv wrapper.

Single-command surface that fetches the latest setforge release from
PyPI, extracts the release notes from the local ``CHANGELOG.md`` (or
the staged source-tree path), assesses schema-impact from the notes
(``NONE`` / ``DETECTED`` / ``UNKNOWN``), and shells out to
``uv tool upgrade setforge`` after an arrow-key radiolist confirm with
three choices: abort (default), upgrade, upgrade + migrate-check.

Flags:

* ``--check`` — read-only PyPI/notes/schema report; no mutation.
* ``--no-prompt`` — accept the recommended choice for automation;
  selects ``upgrade-and-migrate-check`` when schema impact is non-NONE,
  ``upgrade`` otherwise.
* ``--to=X.Y.Z`` — pin the target version (bypasses PyPI selection;
  PyPI is still hit for the version's yanked / prerelease status).
* ``--prerelease`` — include pre-release versions when picking latest.

Output of the success path includes the explicit rollback command
``uv tool install --reinstall --reinstall-package setforge==<prev>``
so the user can revert without leaving the terminal.

The confirm panel ALWAYS surfaces a ``=== schema impact ===`` section
above the radiolist (per user direction 2026-05-19): one of "No schema
change", "SCHEMA CHANGE detected: ...", or "Could not parse schema
impact from release notes" — even when impact is NONE.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import typer
from packaging.version import InvalidVersion, Version
from rich.console import Console
from rich.panel import Panel

from setforge import __version__ as _CURRENT_VERSION
from setforge._changelog_parser import parse_changelog
from setforge._pypi_client import PyPIVersionInfo, fetch_latest_version
from setforge.cli import app
from setforge.cli._help_examples import UPGRADE_EXAMPLES
from setforge.errors import PyPIFetchError, UpgradeError

# Lazy radiolist import — mirrors ``setforge/cli/_confirm.py:34-39``.
# Lets ``setforge --help`` / ``setforge upgrade --help`` skip the
# ~140ms prompt_toolkit cold-start cost; the TUI fires only on the
# interactive confirm path. The module-attribute path
# ``setforge.cli.upgrade.radiolist_dialog`` is preserved so tests
# monkeypatch it the same way the ``_confirm`` tests do.


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "SchemaChangeAssessment",
    "SchemaChangeKind",
    "UpgradeChoice",
    "UpgradePlan",
]

_PACKAGE_NAME: str = "setforge"
_DEFAULT_SCHEMA: str = "1.0"
_SCHEMA_BUMP_RE: re.Pattern[str] = re.compile(
    r"schema_version\s+bumped?\s+(?P<from>\d+\.\d+)\s*(?:→|->)\s*(?P<to>\d+\.\d+)",
    re.IGNORECASE,
)
_BREAKING_SCHEMA_RE: re.Pattern[str] = re.compile(
    r"^\s*[-*]?\s*BREAKING:\s*(?P<body>.*(?:schema|migrate|migration).*)$",
    re.IGNORECASE | re.MULTILINE,
)
_MIGRATE_HINT: str = (
    "   After upgrade, run `setforge migrate --apply` to update your local files."
)
_MANIFEST_LINE_RE: re.Pattern[str] = re.compile(
    r"^\s*[-*]\s*(?:renames?|adds?|removes?|breaking):.*$",
    re.IGNORECASE | re.MULTILINE,
)
_VERSION_RE: re.Pattern[str] = re.compile(r"^\d+\.\d+\.\d+(?:[+\-.][\w.]+)?$")


class UpgradeChoice(StrEnum):
    """Closed set of radiolist outcomes for the upgrade confirm panel."""

    ABORT = "abort"
    UPGRADE = "upgrade"
    UPGRADE_AND_MIGRATE_CHECK = "upgrade-and-migrate-check"


class SchemaChangeKind(StrEnum):
    """Schema-impact verdict assessed from release notes."""

    NONE = "none"
    DETECTED = "detected"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class SchemaChangeAssessment:
    """Pre-upgrade assessment of schema impact, derived from release notes.

    ``impact_summary`` is a human-readable, multiline string. For
    ``NONE``, a fixed reassuring sentence. For ``DETECTED``, a bullet
    list of renames / adds / removes / BREAKING entries extracted
    from the notes. For ``UNKNOWN``, a fixed nudge to run
    ``setforge migrate --check`` after upgrade.
    """

    kind: SchemaChangeKind
    from_schema: str
    to_schema: str | None
    impact_summary: str


@dataclass(slots=True, frozen=True)
class UpgradePlan:
    """Fully-built input to the confirm panel + the wrap.

    Carries the version pair, release notes (extracted from CHANGELOG),
    a flag for major-version bumps, a flag for plain-text ``BREAKING``
    occurrences in the notes, and the schema-impact assessment.
    """

    current_version: str
    target_version: str
    release_notes: str | None
    is_major_bump: bool
    breaking_changes_flagged: bool
    schema_change: SchemaChangeAssessment
    yanked: bool = False
    yanked_reason: str | None = None
    is_prerelease: bool = False
    extra_warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# CHANGELOG resolution
# ---------------------------------------------------------------------------


def _find_changelog() -> Path | None:
    """Locate ``CHANGELOG.md`` in the source tree.

    Walks up from this module's file looking for a ``CHANGELOG.md`` at
    each parent until found (or root). Returns ``None`` when the
    changelog is not bundled — common in installed-wheel layouts where
    the docs are excluded from the wheel.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "CHANGELOG.md"
        if candidate.is_file():
            return candidate
    return None


def _load_release_notes(target_version: str) -> str | None:
    """Load + parse release notes for ``target_version`` from CHANGELOG."""
    changelog_path = _find_changelog()
    if changelog_path is None:
        return None
    try:
        text = changelog_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_changelog(text, target_version)


# ---------------------------------------------------------------------------
# Schema-change assessment
# ---------------------------------------------------------------------------


def _assess_schema_change(
    release_notes: str | None,
    current_schema: str,
    *,
    is_major_bump: bool,
) -> SchemaChangeAssessment:
    """Heuristic-classify schema impact from release notes.

    Heuristic order (per SPEC 3):

    1. Canonical ``schema_version bumped X.Y → A.B`` line → DETECTED
       with from/to extracted.
    2. ``BREAKING:`` line mentioning schema/migrate/migration →
       DETECTED, with the BREAKING block as impact_summary.
    3. No match AND target is the same major version AND notes are
       non-empty → NONE.
    4. No match AND target is a major bump → UNKNOWN (conservative).
    5. Notes unavailable → UNKNOWN.
    """
    if release_notes is None or not release_notes.strip():
        return SchemaChangeAssessment(
            kind=SchemaChangeKind.UNKNOWN,
            from_schema=current_schema,
            to_schema=None,
            impact_summary=(
                "Could not parse schema impact from release notes.\n"
                "   After upgrade, run `setforge migrate --check` to verify."
            ),
        )

    bumped = _SCHEMA_BUMP_RE.search(release_notes)
    if bumped is not None:
        from_v = bumped.group("from")
        to_v = bumped.group("to")
        manifest = _MANIFEST_LINE_RE.findall(release_notes)
        summary = f"SCHEMA CHANGE detected: {from_v} → {to_v}"
        if manifest:
            joined = "\n".join(
                f"   • {line.strip().lstrip('-*').strip()}" for line in manifest
            )
            summary = f"{summary}\n{joined}"
        summary = f"{summary}\n{_MIGRATE_HINT}"
        return SchemaChangeAssessment(
            kind=SchemaChangeKind.DETECTED,
            from_schema=from_v,
            to_schema=to_v,
            impact_summary=summary,
        )

    breaking = _BREAKING_SCHEMA_RE.search(release_notes)
    if breaking is not None:
        body = breaking.group("body").strip()
        summary = f"SCHEMA CHANGE detected (BREAKING):\n   • {body}\n{_MIGRATE_HINT}"
        return SchemaChangeAssessment(
            kind=SchemaChangeKind.DETECTED,
            from_schema=current_schema,
            to_schema=None,
            impact_summary=summary,
        )

    if is_major_bump:
        return SchemaChangeAssessment(
            kind=SchemaChangeKind.UNKNOWN,
            from_schema=current_schema,
            to_schema=None,
            impact_summary=(
                "Major-version bump with no explicit schema signal in release notes.\n"
                "   After upgrade, run `setforge migrate --check` to verify."
            ),
        )

    return SchemaChangeAssessment(
        kind=SchemaChangeKind.NONE,
        from_schema=current_schema,
        to_schema=None,
        impact_summary=(
            "No schema change. Fully backwards compatible "
            "— no actions required after upgrade."
        ),
    )


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def _is_major_bump(current: str, target: str) -> bool:
    """Return True when ``target`` is a strictly-greater major version."""
    try:
        return Version(target).major > Version(current).major
    except InvalidVersion:
        return False


def _build_upgrade_plan(*, to: str | None, prerelease: bool) -> UpgradePlan:
    """Resolve target version + load notes + assess schema → UpgradePlan."""
    if to is not None and not _VERSION_RE.match(to):
        raise UpgradeError(f"--to value {to!r} is not a valid X.Y.Z version string")
    info: PyPIVersionInfo = fetch_latest_version(
        package=_PACKAGE_NAME,
        current_version=_CURRENT_VERSION,
        include_prereleases=prerelease or (to is not None and _looks_prerelease(to)),
    )
    target = to if to is not None else info.version
    notes = _load_release_notes(target)
    is_major = _is_major_bump(_CURRENT_VERSION, target)
    breaking_flag = notes is not None and "BREAKING" in notes
    schema = _assess_schema_change(
        notes,
        current_schema=_DEFAULT_SCHEMA,
        is_major_bump=is_major,
    )
    warnings: list[str] = []
    if to is not None and to != info.version:
        warnings.append(
            f"--to={to} pins a version other than PyPI latest ({info.version})."
        )
    return UpgradePlan(
        current_version=_CURRENT_VERSION,
        target_version=target,
        release_notes=notes,
        is_major_bump=is_major,
        breaking_changes_flagged=breaking_flag,
        schema_change=schema,
        yanked=info.yanked,
        yanked_reason=info.yanked_reason,
        is_prerelease=info.is_prerelease,
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


def _format_schema_impact(assessment: SchemaChangeAssessment) -> str:
    """Render the always-present schema-impact section for the panel."""
    if assessment.kind is SchemaChangeKind.NONE:
        symbol = "[green]✓[/green]"
    elif assessment.kind is SchemaChangeKind.DETECTED:
        symbol = "[yellow]⚠[/yellow]"
    else:
        symbol = "[cyan]?[/cyan]"
    return f"=== schema impact ===\n{symbol} {assessment.impact_summary}"


def _render_confirm_panel(plan: UpgradePlan, *, console: Console) -> None:
    """Print the pre-confirm panel: header + notes + schema + warnings."""
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
    if plan.breaking_changes_flagged:
        console.print(
            "[bold red]BREAKING:[/bold red] release notes contain a "
            "BREAKING marker — review carefully."
        )
    for warning in plan.extra_warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")

    if plan.release_notes:
        console.print("[bold]release notes:[/bold]")
        console.print(plan.release_notes)
    else:
        console.print(
            "[dim]no release notes found in CHANGELOG.md for "
            f"{plan.target_version}.[/dim]"
        )

    console.print(_format_schema_impact(plan.schema_change))


def _confirm_upgrade(plan: UpgradePlan, *, yes: bool) -> UpgradeChoice:
    """Render the panel and prompt arrow-key choice; return the user's pick.

    ``yes=True`` (``--no-prompt``) auto-picks the recommended choice:
    ``UPGRADE_AND_MIGRATE_CHECK`` when schema impact is non-NONE,
    ``UPGRADE`` otherwise. Esc / None from the dialog → ABORT.
    """
    recommend_migrate = plan.schema_change.kind is not SchemaChangeKind.NONE
    default_choice = (
        UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK
        if recommend_migrate
        else UpgradeChoice.UPGRADE
    )
    if yes:
        return default_choice

    console = Console()
    _render_confirm_panel(plan, console=console)

    from setforge.cli import upgrade as _self  # local alias for monkeypatch

    choice = _self.radiolist_dialog(
        title="setforge upgrade",
        text="Proceed?",
        values=[
            (UpgradeChoice.ABORT, "Abort — no changes"),
            (UpgradeChoice.UPGRADE, "Upgrade"),
            (
                UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK,
                "Upgrade + run `setforge migrate --check`",
            ),
        ],
        default=default_choice,
    ).run()
    if choice is None:
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
    the no-op case where setforge is already at the latest pinned
    version. The reliable signal is STDOUT — match
    ``"Nothing to upgrade"`` or ``"already up to date"``.
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
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise UpgradeError(
            f"uv tool upgrade failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    stdout_lower = result.stdout.lower()
    if "nothing to upgrade" in stdout_lower or "already up to date" in stdout_lower:
        typer.echo(
            f"setforge is already on the requested version "
            f"({target}); no-op (pin in effect)."
        )


def _verify_post_upgrade(*, expected: str) -> None:
    """Run ``uv tool list`` and assert ``setforge\\s+<expected>`` is present."""
    uv = shutil.which("uv")
    if uv is None:
        raise UpgradeError("uv vanished from PATH between upgrade and verify")
    result = subprocess.run(
        [uv, "tool", "list"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise UpgradeError(
            f"uv tool list failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    pattern = re.compile(
        rf"^{re.escape(_PACKAGE_NAME)}\s+{re.escape(expected)}\b",
        re.MULTILINE,
    )
    if pattern.search(result.stdout) is None:
        raise UpgradeError(
            f"post-upgrade verification: did not see "
            f"`{_PACKAGE_NAME} {expected}` in `uv tool list` output"
        )


def _run_migrate_check_subprocess() -> None:
    """Best-effort ``uv run setforge migrate --check``; soft-fail when missing.

    ``migrate`` is registered by a SIBLING bd issue (setforge-s5pq); when
    it has not landed yet the subprocess exits non-zero with a "no such
    command" message. Soft-fail in that case — print a hint and return.
    """
    uv = shutil.which("uv")
    if uv is None:
        typer.echo("uv missing — skipping migrate --check.")
        return
    result = subprocess.run(
        [uv, "run", _PACKAGE_NAME, "migrate", "--check"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
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
    console = Console()
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


# ---------------------------------------------------------------------------
# Typer entry point
# ---------------------------------------------------------------------------


@app.command(epilog=UPGRADE_EXAMPLES)
def upgrade(
    check: bool = typer.Option(
        False,
        "--check",
        help="Read-only: report current vs latest + release notes; no mutation.",
    ),
    no_prompt: bool = typer.Option(
        False,
        "--no-prompt",
        help="Skip the radiolist confirm; pick the recommended choice.",
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
) -> None:
    """Upgrade setforge: PyPI check + release notes + uv wrapper (mockup U)."""
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
        # Automation path: skip the panel render, take the recommended
        # choice, run the wrap. Tests cover both branches.
        choice = _confirm_upgrade(plan, yes=True)
    else:
        choice = _confirm_upgrade(plan, yes=no_prompt)

    if choice is UpgradeChoice.ABORT:
        return

    _run_uv_tool_upgrade(target=plan.target_version, pinned=to is not None)
    _verify_post_upgrade(expected=plan.target_version)

    if choice is UpgradeChoice.UPGRADE_AND_MIGRATE_CHECK:
        _run_migrate_check_subprocess()

    _print_completion_report(plan)
