"""``setforge init`` — bootstrap config dirs + local.yaml template + env health.

Mockup J (user-approved 2026-05-18). Three radiolist_dialog prompts:
source-config (skip/git/path), apply-confirm (proceed/abort),
``--force`` confirm (abort/overwrite+backup/overwrite+no-backup).
Reinit is idempotent and content-aware — re-running without
``--force`` rechecks the environment and surfaces newly-enabled
capabilities (mockup scenario 2) without overwriting local.yaml.
"""

from __future__ import annotations

import shutil
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any, assert_never

import typer
from rich.console import Console

from setforge.binaries import _STUB_TEMPLATE, LOCAL_CONFIG_PATH
from setforge.cli import app
from setforge.cli._init_helpers import (
    BinaryProbe,
    CapabilityProbe,
    CapabilityState,
    DirProbe,
    EnvProbe,
    _mkdir_with_retry,
    backup_suffix_now,
    config_dir_path,
    host_local_dir_path,
    is_initialized,
    probe_environment,
)

# prompt_toolkit's ``radiolist_dialog`` resolves through this module's
# lazy ``__getattr__`` below so cold-start commands (``setforge --help``,
# ``setforge validate``) skip the ~140ms prompt_toolkit import. Tests
# monkeypatch ``setforge.cli.init.radiolist_dialog`` directly through the
# same attribute path; mirror :mod:`setforge.cli._confirm` and
# :mod:`setforge.cli.section` exactly.


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ApplyChoice", "ForceChoice", "SourceChoice", "init"]


class SourceChoice(StrEnum):
    """Outcome of the source-config sub-prompt."""

    SKIP = "skip"
    GIT = "git"
    PATH = "path"


class ApplyChoice(StrEnum):
    """Outcome of the apply-confirm prompt."""

    PROCEED = "proceed"
    ABORT = "abort"


class ForceChoice(StrEnum):
    """Outcome of the ``--force`` overwrite confirm prompt."""

    ABORT = "abort"
    OVERWRITE_WITH_BACKUP = "overwrite-with-backup"
    OVERWRITE_NO_BACKUP = "overwrite-no-backup"


def _render_env_section(probe: EnvProbe, *, console: Console) -> None:
    """Print the ``checking environment...`` block from mockup J."""
    console.print("checking environment...")
    for binary in probe.binaries:
        _render_binary_row(binary, console=console)
    if any(not b.resolved_path and not b.required for b in probe.binaries):
        console.print(
            "  NOTE: missing optional binaries are NOT blockers. Init proceeds."
        )


def _render_binary_row(probe: BinaryProbe, *, console: Console) -> None:
    """Render one binary's status line(s) — ✓ on resolve, ⚠ + impact + fix on absent."""
    if probe.resolved_path is not None:
        console.print(f"  [green]✓[/green] {probe.name} binary on PATH")
        return
    if probe.required:
        console.print(f"  [red]✗[/red] {probe.name} binary not on PATH (REQUIRED)")
        console.print(f"        fix: {probe.fix_hint}")
        return
    console.print(f"  [yellow]⚠[/yellow] {probe.name} binary not on PATH")
    impact = _impact_for(probe.name)
    console.print(f"        impact: {impact}")
    console.print(f"        fix: {probe.fix_hint}")


def _impact_for(binary_name: str) -> str:
    """Return the human-readable impact line for a missing optional binary."""
    if binary_name == "claude":
        return "Claude plugin install/management DISABLED at runtime."
    if binary_name == "code":
        return "VSCode extension install/management DISABLED at runtime."
    return f"{binary_name}-related capabilities DISABLED at runtime."


def _render_dirs_section(probe: EnvProbe, *, console: Console) -> None:
    """Print the ``checking config directories...`` block from mockup J."""
    console.print("checking config directories...")
    for d in probe.dirs:
        _render_dir_row(d, console=console)


def _render_dir_row(probe: DirProbe, *, console: Console) -> None:
    """Render one dir/file existence row."""
    if probe.exists:
        console.print(f"  [green]✓[/green] {probe.path} exists")
        return
    console.print(f"  [red]✗[/red] {probe.path} does not exist")


def _render_capabilities_section(probe: EnvProbe, *, console: Console) -> None:
    """Print the ``=== capabilities ===`` table from mockup J."""
    console.print("=== capabilities ===")
    for cap in probe.capabilities:
        _render_capability_row(cap, console=console)


def _render_capability_row(probe: CapabilityProbe, *, console: Console) -> None:
    """Render one capability row — ✓ enabled / ✗ DISABLED (reason)."""
    if probe.state is CapabilityState.ENABLED:
        marker = " [yellow]★ NEWLY ENABLED[/yellow]" if probe.newly_enabled else ""
        console.print(f"  [green]✓[/green] {probe.label}{marker}")
        return
    console.print(f"  [red]✗[/red] {probe.label}        DISABLED {probe.reason}")


def _print_check_report(probe: EnvProbe, *, console: Console) -> None:
    """Render the full ``--check`` read-only health report."""
    console.print("=== setforge init --check ===")
    _render_env_section(probe, console=console)
    _render_dirs_section(probe, console=console)
    _render_capabilities_section(probe, console=console)
    console.print("=== check complete (read-only; no changes made) ===")


def _print_will_create_panel(probe: EnvProbe, *, console: Console) -> None:
    """Render the ``=== this init will create ===`` block."""
    to_create = [d for d in probe.dirs if d.will_create]
    if not to_create:
        return
    console.print("=== this init will create ===")
    for d in to_create:
        console.print(f"  {d.path}")


def _prompt_source_config(
    *,
    no_prompt: bool,
    path_source: Path | None,
    git_source: str | None,
) -> SourceChoice:
    """Resolve the source-config sub-prompt → SourceChoice.

    Non-interactive precedence: ``--path-source`` > ``--git-source`` >
    default ``SKIP`` (when ``--no-prompt`` is set without a source flag,
    the user opted out of configuring a source). Interactive flow goes
    through :func:`radiolist_dialog` with arrow-key selection per
    mockup J line 553.
    """
    if path_source is not None:
        return SourceChoice.PATH
    if git_source is not None:
        return SourceChoice.GIT
    if no_prompt:
        return SourceChoice.SKIP
    from setforge.cli import init as _self

    result = _self.radiolist_dialog(
        title="configure your config-repo source?",
        text=(
            "skip = configure later (edit local.yaml's source: block by hand)\n"
            "git  = clone a remote config repo now\n"
            "path = point to a local config-repo directory now"
        ),
        values=[
            (SourceChoice.SKIP, "skip (default)"),
            (SourceChoice.GIT, "git URL"),
            (SourceChoice.PATH, "local path"),
        ],
        default=SourceChoice.SKIP,
    ).run()
    if result is None:
        return SourceChoice.SKIP
    assert isinstance(result, SourceChoice)
    return result


def _prompt_apply_confirm(*, no_prompt: bool) -> ApplyChoice:
    """Resolve the ``=== ready to apply ===`` prompt → ApplyChoice."""
    if no_prompt:
        return ApplyChoice.PROCEED
    from setforge.cli import init as _self

    result = _self.radiolist_dialog(
        title="ready to apply?",
        text="proceed creates the directories above; abort makes no changes",
        values=[
            (ApplyChoice.PROCEED, "proceed"),
            (ApplyChoice.ABORT, "abort"),
        ],
        default=ApplyChoice.PROCEED,
    ).run()
    if result is None:
        return ApplyChoice.ABORT
    assert isinstance(result, ApplyChoice)
    return result


def _prompt_force_confirm(*, no_prompt: bool) -> ForceChoice:
    """Resolve the ``--force`` overwrite confirm → ForceChoice.

    With ``--no-prompt`` the user is taking responsibility for the
    backup decision; default to ``OVERWRITE_WITH_BACKUP`` (the safer
    choice) per the mockup-J "abort default" overridden by the
    explicit ``--force`` flag.
    """
    if no_prompt:
        return ForceChoice.OVERWRITE_WITH_BACKUP
    from setforge.cli import init as _self

    result = _self.radiolist_dialog(
        title="setforge init --force",
        text=(
            "DESTRUCTIVE: this will overwrite ~/.config/setforge/local.yaml.\n"
            "Pick a recovery option:"
        ),
        values=[
            (ForceChoice.ABORT, "abort (default — no changes)"),
            (
                ForceChoice.OVERWRITE_WITH_BACKUP,
                "overwrite + back up existing files",
            ),
            (
                ForceChoice.OVERWRITE_NO_BACKUP,
                "overwrite + no backup (existing content discarded)",
            ),
        ],
        default=ForceChoice.ABORT,
    ).run()
    if result is None:
        return ForceChoice.ABORT
    assert isinstance(result, ForceChoice)
    return result


def _backup_existing(*, console: Console) -> None:
    """Rename existing local.yaml to ``<file>.bak.<UTC-ISO8601>`` if present.

    Backup files are NEVER auto-deleted — user controls cleanup
    (research brief §7, scope note). Restore is a copy operation the
    user runs by hand; we do not ship ``init --restore-backup`` in
    this bead.
    """
    if not LOCAL_CONFIG_PATH.exists():
        return
    suffix = backup_suffix_now()
    backup = LOCAL_CONFIG_PATH.with_name(f"{LOCAL_CONFIG_PATH.name}.bak.{suffix}")
    shutil.copy2(LOCAL_CONFIG_PATH, backup)
    console.print(f"  backed up {LOCAL_CONFIG_PATH.name} → {backup.name}")


def _apply_bootstrap(probe: EnvProbe, *, console: Console) -> None:
    """Create the three init paths + write the local.yaml stub.

    Uses :func:`_mkdir_with_retry` for the TOCTOU-resilient idempotent
    mkdir per research brief §7. The root callback's
    ``ensure_local_config_stub`` may have already created
    ``LOCAL_CONFIG_PATH``; this function rewrites it unconditionally
    (callers gate on ``--force`` semantics upstream).
    """
    console.print("=== applying ===")
    _mkdir_with_retry(config_dir_path())
    _mkdir_with_retry(host_local_dir_path())
    LOCAL_CONFIG_PATH.write_text(_STUB_TEMPLATE, encoding="utf-8")
    created = [d.path for d in probe.dirs if d.will_create]
    if created:
        names = " + ".join(str(p) for p in created)
        console.print(f"  created {names}")
    else:
        console.print("  (no new files created)")


def _print_completion_report(
    *,
    source_choice: SourceChoice,
    console: Console,
) -> None:
    """Render the ``=== init complete ===`` next-steps block."""
    console.print("=== init complete ===")
    console.print(
        "  next steps: edit local.yaml source: block; "
        "setforge validate --list-profiles;"
    )
    console.print("              setforge install --profile=<name> --dry-run")
    console.print(
        "  to undo: rm -rf ~/.config/setforge ~/.local/share/setforge/host-local"
    )
    _ = source_choice  # source-config wiring is a follow-up bead (n2la out-of-scope)


def _print_idempotent_reinit_report(probe: EnvProbe, *, console: Console) -> None:
    """Render the no-changes reinit report (mockup J scenario 2)."""
    _render_env_section(probe, console=console)
    _render_dirs_section(probe, console=console)
    _render_capabilities_section(probe, console=console)
    newly = sum(1 for c in probe.capabilities if c.newly_enabled)
    console.print("=== nothing to create — local.yaml customizations preserved ===")
    console.print(
        "  (reinit is purely informational; capability detection happens "
        "at install/sync time)"
    )
    if newly:
        suffix = "y" if newly == 1 else "ies"
        console.print(
            f"=== init exit (no changes; {newly} capabilit{suffix} newly available) ==="
        )
    else:
        console.print("=== init exit (no changes) ===")


def _handle_check_mode(*, console: Console) -> None:
    """``--check`` path: read-only health report, no side effects."""
    probe = probe_environment()
    _print_check_report(probe, console=console)


def _handle_force_mode(
    *,
    no_prompt: bool,
    source_choice: SourceChoice,
    console: Console,
) -> int:
    """``--force`` path: confirm + (optional) backup + rewrite. Returns exit code."""
    force_choice = _prompt_force_confirm(no_prompt=no_prompt)
    match force_choice:
        case ForceChoice.ABORT:
            console.print("[red]✗ aborted[/red] — no changes")
            return 0
        case ForceChoice.OVERWRITE_WITH_BACKUP:
            _backup_existing(console=console)
        case ForceChoice.OVERWRITE_NO_BACKUP:
            pass
        case _ as unreachable:
            assert_never(unreachable)
    probe = probe_environment()
    _apply_bootstrap(probe, console=console)
    _print_completion_report(source_choice=source_choice, console=console)
    return 0


def _handle_fresh_init(
    *,
    no_prompt: bool,
    source_choice: SourceChoice,
    console: Console,
) -> int:
    """Fresh-init path: render plan, confirm, apply. Returns exit code."""
    probe = probe_environment()
    _render_env_section(probe, console=console)
    _render_dirs_section(probe, console=console)
    _render_capabilities_section(probe, console=console)
    _print_will_create_panel(probe, console=console)
    apply_choice = _prompt_apply_confirm(no_prompt=no_prompt)
    if apply_choice is ApplyChoice.ABORT:
        console.print("[red]✗ aborted[/red] — no changes")
        return 0
    _apply_bootstrap(probe, console=console)
    _print_completion_report(source_choice=source_choice, console=console)
    return 0


@app.command()
def init(
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Overwrite an existing local.yaml (gated by an arrow-key "
            "confirm with backup option)."
        ),
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help=(
            "Read-only health check — print env / dirs / capabilities; make no changes."
        ),
    ),
    no_prompt: bool = typer.Option(
        False,
        "--no-prompt",
        help=(
            "Non-interactive mode; default-proceed at every prompt "
            "(for CI / automation)."
        ),
    ),
    path_source: Path | None = typer.Option(
        None,
        "--path-source",
        help=(
            "Pre-select the 'path' source-config option (skips the source sub-prompt)."
        ),
    ),
    git_source: str | None = typer.Option(
        None,
        "--git-source",
        help=(
            "Pre-select the 'git' source-config option (skips the source sub-prompt)."
        ),
    ),
    git_ref: str = typer.Option(
        "main",
        "--git-ref",
        help="Ref to clone when --git-source is supplied (default: main).",
    ),
) -> None:
    """Bootstrap setforge config dirs + local.yaml template + env health.

    See mockup J in
    ``~/.claude/projects/-home-raul-setforge/specs/2026-05-18-release-blocker-workflows.md``
    for the four scenarios (fresh init / reinit / --force / --check).
    """
    console = Console()
    console.print("=== setforge init ===")
    if check:
        _handle_check_mode(console=console)
        return

    probe = probe_environment()
    if is_initialized(probe) and not force:
        _print_idempotent_reinit_report(probe, console=console)
        return

    source_choice = _prompt_source_config(
        no_prompt=no_prompt,
        path_source=path_source,
        git_source=git_source,
    )
    _ = git_ref  # consumed downstream when source-config plumbing lands

    if force:
        exit_code = _handle_force_mode(
            no_prompt=no_prompt,
            source_choice=source_choice,
            console=console,
        )
    else:
        exit_code = _handle_fresh_init(
            no_prompt=no_prompt,
            source_choice=source_choice,
            console=console,
        )
    if exit_code != 0:
        sys.exit(exit_code)
