"""``setforge init`` — bootstrap config dirs + local.yaml template + env health.

Mockup J (user-approved 2026-05-18). Three radiolist_dialog prompts:
source-config (skip/git/path), apply-confirm (proceed/abort),
``--force`` confirm (abort/overwrite+backup/overwrite+no-backup).
Reinit is idempotent and content-aware — re-running without
``--force`` rechecks the environment and surfaces newly-enabled
capabilities (mockup scenario 2) without overwriting local.yaml.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, assert_never

import typer
from rich.console import Console

from setforge.binaries import _STUB_TEMPLATE, LOCAL_CONFIG_PATH
from setforge.cli import app
from setforge.cli._config_repo import (
    ConfigRepoScaffoldError,
    default_config_repo_dir,
    local_yaml_has_source,
    scaffold_config_repo,
)
from setforge.cli._help_examples import INIT_EXAMPLES
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
from setforge.errors import ConfirmRequiresInteractive

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
    if name == "input_dialog":
        from prompt_toolkit.shortcuts import input_dialog

        return input_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ApplyChoice",
    "ForceChoice",
    "SourceChoice",
    "SourceSpec",
    "init",
]


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


@dataclass(slots=True, frozen=True)
class SourceSpec:
    """Outcome of source-config resolution: choice plus per-kind fields.

    ``choice`` is the user-facing decision; ``path`` is set for
    :attr:`SourceChoice.PATH`; ``url`` and ``ref`` are set for
    :attr:`SourceChoice.GIT`. :attr:`SourceChoice.SKIP` leaves all
    optional fields ``None``/default.
    """

    choice: SourceChoice
    path: Path | None = None
    url: str | None = None
    ref: str = "main"


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
    git_ref: str,
) -> SourceSpec:
    """Resolve the source-config sub-prompt → SourceSpec.

    Non-interactive precedence: ``--path-source`` > ``--git-source`` >
    default ``SKIP`` (when ``--no-prompt`` is set without a source flag,
    the user opted out of configuring a source). Interactive flow goes
    through :func:`radiolist_dialog` with arrow-key selection per
    mockup J line 553.

    Returns a :class:`SourceSpec` carrying both the choice and the
    per-kind fields (path / url+ref) needed to write a ``source:``
    block into ``local.yaml``. Interactive GIT/PATH selections collect
    the URL / directory via a follow-up :func:`input_dialog`; an empty
    or cancelled entry falls back to :attr:`SourceChoice.SKIP` so the
    stub stays editable rather than half-written.
    """
    if path_source is not None:
        return SourceSpec(
            choice=SourceChoice.PATH,
            path=path_source,
        )
    if git_source is not None:
        return SourceSpec(
            choice=SourceChoice.GIT,
            url=git_source,
            ref=git_ref,
        )
    if no_prompt:
        return SourceSpec(choice=SourceChoice.SKIP)
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            "setforge init requires --no-prompt when stdin is not a TTY"
        )
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
    if result is SourceChoice.GIT:
        url = _self.input_dialog(
            title="git config-repo source",
            text="Enter the git URL to clone (blank to skip):",
        ).run()
        # Strip first: a whitespace-only entry is truthy but would write a
        # `path:`/`url:` plain scalar that YAML re-reads as null — a
        # half-written stub. Collapse it (and None/empty) to SKIP.
        url = (url or "").strip()
        if not url:
            return SourceSpec(choice=SourceChoice.SKIP)
        return SourceSpec(choice=SourceChoice.GIT, url=url, ref=git_ref)
    if result is SourceChoice.PATH:
        path_str = _self.input_dialog(
            title="local config-repo source",
            text="Enter the local config-repo directory (blank to skip):",
        ).run()
        path_str = (path_str or "").strip()
        if not path_str:
            return SourceSpec(choice=SourceChoice.SKIP)
        return SourceSpec(choice=SourceChoice.PATH, path=Path(path_str))
    # SKIP selection or None (cancel/escape).
    return SourceSpec(choice=SourceChoice.SKIP)


def _prompt_apply_confirm(*, no_prompt: bool) -> ApplyChoice:
    """Resolve the ``=== ready to apply ===`` prompt → ApplyChoice."""
    if no_prompt:
        return ApplyChoice.PROCEED
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            "setforge init requires --no-prompt when stdin is not a TTY"
        )
    from setforge.cli import init as _self

    result = _self.radiolist_dialog(
        title="ready to apply?",
        text=(
            "proceed creates the directories above and (re)writes "
            "local.yaml — a customized local.yaml is backed up to a .bak "
            "first; abort makes no changes"
        ),
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
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            "setforge init requires --no-prompt when stdin is not a TTY"
        )
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


def _backup_existing(*, console: Console) -> Path | None:
    """Rename existing local.yaml to ``<file>.bak.<UTC-ISO8601>`` if present.

    Returns the backup file path (so callers can surface a
    restore-from-backup hint) or ``None`` when no backup was made
    because no existing file was found. Backup files are NEVER
    auto-deleted — user controls cleanup (research brief §7, scope
    note). Restore is a copy operation the user runs by hand; we do
    not ship ``init --restore-backup`` in this bead.
    """
    if not LOCAL_CONFIG_PATH.exists():
        return None
    suffix = backup_suffix_now()
    backup = LOCAL_CONFIG_PATH.with_name(f"{LOCAL_CONFIG_PATH.name}.bak.{suffix}")
    shutil.copy2(LOCAL_CONFIG_PATH, backup)
    console.print(f"  backed up {LOCAL_CONFIG_PATH.name} → {backup.name}")
    return backup


def _build_source_block(spec: SourceSpec) -> str:
    """Render the ``source:`` block to append to the local.yaml stub.

    Returns an empty string for :attr:`SourceChoice.SKIP` (the stub's
    existing instructions tell the user how to edit ``source:`` by
    hand). For PATH/GIT, returns a leading blank line plus a literal
    YAML mapping that matches the
    :class:`setforge.source._LocalSourceConfig` schema. We do not
    round-trip through ruamel.yaml because the stub is a heavily
    commented template that a YAML emitter would not reproduce
    faithfully — appending a literal snippet is the lower-risk shape.
    """
    match spec.choice:
        case SourceChoice.SKIP:
            return ""
        case SourceChoice.PATH:
            assert spec.path is not None, "PATH choice requires path"
            return (
                "\n"
                "# Pre-configured by `setforge init --path-source`:\n"
                "source:\n"
                "  kind: path\n"
                f"  path: {json.dumps(str(spec.path))}\n"
            )
        case SourceChoice.GIT:
            assert spec.url is not None, "GIT choice requires url"
            return (
                "\n"
                "# Pre-configured by `setforge init --git-source`:\n"
                "source:\n"
                "  kind: git\n"
                f"  url: {json.dumps(spec.url)}\n"
                f"  ref: {json.dumps(spec.ref)}\n"
            )
        case _ as unreachable:
            assert_never(unreachable)


def _wire_source_block(target_dir: Path, *, console: Console) -> bool:
    """Append a path ``source:`` block pointing at ``target_dir``, dedup-guarded.

    Returns ``True`` when the block was appended, ``False`` when
    ``local.yaml`` already carried a ``source:`` key (the dedup guard: a
    second ``init --config-repo`` run must leave ``local.yaml``
    byte-identical). The append preserves the file's existing permission
    bits — it must never widen a ``0600`` stub to ``0644``. The new bytes
    are staged into a temp file in the destination's own directory (never
    ``/tmp``, to avoid a cross-device rename), then atomically renamed over
    the target.
    """
    if local_yaml_has_source(LOCAL_CONFIG_PATH):
        console.print("  source: block already present — left unchanged")
        return False
    spec = SourceSpec(choice=SourceChoice.PATH, path=target_dir)
    existing = LOCAL_CONFIG_PATH.read_text(encoding="utf-8")
    new_content = existing + _build_source_block(spec)
    mode = LOCAL_CONFIG_PATH.stat().st_mode
    tmp = LOCAL_CONFIG_PATH.with_name(f"{LOCAL_CONFIG_PATH.name}.tmp")
    try:
        tmp.write_text(new_content, encoding="utf-8")
        tmp.chmod(mode)
        tmp.replace(LOCAL_CONFIG_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    console.print(f"  wired local.yaml source: → {target_dir}")
    return True


def _prompt_config_repo_dir(*, no_prompt: bool) -> Path:
    """Resolve the config-repo target directory.

    Default is ``~/projects/<name>-config`` per
    :func:`setforge.cli._config_repo.default_config_repo_dir`. Under
    ``--no-prompt`` the default is taken verbatim. Interactively, an
    :func:`input_dialog` collects the directory (blank → default). A
    non-TTY without ``--no-prompt`` raises
    :class:`ConfirmRequiresInteractive` consistent with the other prompts.
    """
    default = default_config_repo_dir()
    if no_prompt:
        return default
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            "setforge init requires --no-prompt when stdin is not a TTY"
        )
    from setforge.cli import init as _self

    entered = _self.input_dialog(
        title="config-repo directory",
        text=f"Where to scaffold the config repo? (blank = {default}):",
    ).run()
    entered = (entered or "").strip()
    if not entered:
        return default
    return Path(entered).expanduser()


def _handle_config_repo(*, no_prompt: bool, console: Console) -> int:
    """``--config-repo`` path: ensure host-local, scaffold repo, wire source.

    Returns an exit code. The host-local bootstrap runs first when the
    host-local layer is not yet fully initialized (idempotent reuse
    otherwise), then the config-repo layer is scaffolded and
    ``local.yaml``'s ``source:`` block is wired at it (dedup-guarded). A
    scaffold failure surfaces as a clean ``error:`` line and exit 1 rather
    than a traceback. The exit-code contract covers scaffold failures only:
    a non-interactive terminal without ``--no-prompt`` raises
    :exc:`ConfirmRequiresInteractive` from the dir prompt before scaffolding,
    which propagates to the top-level CLI handler.

    The gate is :func:`is_initialized`, NOT a bare ``local.yaml`` existence
    check: the Typer root callback writes the ``local.yaml`` stub on every
    invocation, so an existence check would always skip the bootstrap and
    leave the host-local share dir uncreated. ``is_initialized`` (sentinel
    file AND host-local dir both present) is the same gate bare ``init``
    uses, so ``--config-repo`` performs the identical host-local bootstrap.
    """
    probe = probe_environment()
    if is_initialized(probe):
        console.print("  host-local layer already initialized — reusing")
    else:
        _apply_bootstrap(
            probe, source_spec=SourceSpec(choice=SourceChoice.SKIP), console=console
        )
    target_dir = _prompt_config_repo_dir(no_prompt=no_prompt)
    try:
        scaffolded = scaffold_config_repo(target_dir)
    except ConfigRepoScaffoldError as err:
        console.print(f"[red]error:[/red] {err}")
        return 1
    console.print(f"  scaffolded config repo at {scaffolded}")
    _wire_source_block(scaffolded, console=console)
    console.print("=== init --config-repo complete ===")
    console.print(
        "  next steps: setforge validate --all; add tracked files to setforge.yaml"
    )
    return 0


#: Marker comment prefix that ``_build_source_block`` emits for an
#: init-generated ``source:`` block. A bare ``startswith(_STUB_TEMPLATE)``
#: check cannot tell an init-written overlay apart from a user-appended one
#: (the stub instructs users to add their own ``source:`` block at the end);
#: the marker is what distinguishes "init wrote this" from customization.
_SOURCE_BLOCK_MARKER = "\n# Pre-configured by `setforge init"


def _local_yaml_is_pristine_stub() -> bool:
    """Return True iff the existing local.yaml is an untouched stub.

    The root Typer callback writes ``_STUB_TEMPLATE`` on every invocation,
    and the PATH/GIT init paths append a generated ``source:`` block to it.
    A file whose content is exactly the stub — or the stub followed solely
    by an init-generated, marker-tagged source block — carries no user
    customization and is safe to overwrite. Any other content — a
    hand-edited ``binaries:`` block, a custom or hand-appended ``source:``,
    plugin/extension overlays — must be preserved.

    A bare ``startswith(_STUB_TEMPLATE)`` would misclassify a stub plus a
    user-appended ``source:``/``plugins:``/``extensions:`` block (exactly
    what the stub's own instructions tell users to add) as pristine and
    overwrite it without a backup. We therefore require the suffix after the
    stub to be empty or an init-written, marker-tagged source block.

    Returns True when the file is absent (nothing to clobber); False when it
    holds customized content that an overwrite would destroy.
    """
    if not LOCAL_CONFIG_PATH.exists():
        return True
    try:
        text = LOCAL_CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        # Unreadable file: treat as customized so we err on the safe side
        # (back it up rather than silently discard).
        return False
    if not text.startswith(_STUB_TEMPLATE):
        return False
    suffix = text[len(_STUB_TEMPLATE) :]
    # Exactly the stub, or the stub plus an init-generated source block.
    return suffix == "" or suffix.startswith(_SOURCE_BLOCK_MARKER)


def _apply_bootstrap(
    probe: EnvProbe,
    *,
    source_spec: SourceSpec,
    console: Console,
    force: bool = False,
) -> None:
    """Create the three init paths + write the local.yaml stub.

    Uses :func:`_mkdir_with_retry` for the TOCTOU-resilient idempotent
    mkdir per research brief §7. The root callback's
    ``ensure_local_config_stub`` may have already created
    ``LOCAL_CONFIG_PATH``; this function rewrites it. When
    ``source_spec`` carries a PATH or GIT choice, appends a
    pre-configured ``source:`` block to the stub.

    ``force=True`` means the caller already resolved the overwrite/backup
    decision (the ``--force`` flow), so the rewrite is unconditional.
    ``force=False`` (the auto-bootstrap flows reached when the host-local
    layer is not yet initialized) MUST NOT silently clobber a customized
    local.yaml: a non-pristine-stub file is snapshotted to a timestamped
    ``.bak`` via :func:`_backup_existing` before being rewritten, so a
    hand-edited config is always recoverable.
    """
    console.print("=== applying ===")
    _mkdir_with_retry(config_dir_path())
    _mkdir_with_retry(host_local_dir_path())
    if not force and not _local_yaml_is_pristine_stub():
        _backup_existing(console=console)
    LOCAL_CONFIG_PATH.write_text(
        _STUB_TEMPLATE + _build_source_block(source_spec), encoding="utf-8"
    )
    created = [d.path for d in probe.dirs if d.will_create]
    if created:
        names = " + ".join(str(p) for p in created)
        console.print(f"  created {names}")
    else:
        console.print("  (no new files created)")


def _print_completion_report(
    *,
    source_spec: SourceSpec,
    backup_path: Path | None = None,
    console: Console,
) -> None:
    """Render the ``=== init complete ===`` next-steps block.

    When ``backup_path`` is supplied (the ``--force`` overwrite+backup
    branch took a snapshot), surface a copy-back command so the user
    knows how to roll back without hunting for the backup name.
    """
    console.print("=== init complete ===")
    if source_spec.choice is SourceChoice.SKIP:
        console.print(
            "  next steps: edit local.yaml source: block; "
            "setforge validate --list-profiles;"
        )
    else:
        console.print(
            "  next steps: setforge validate --list-profiles; (source: pre-configured)"
        )
    console.print("              setforge install --profile=<name> --dry-run")
    console.print(
        "  to undo: rm -rf ~/.config/setforge ~/.local/share/setforge/host-local"
    )
    if backup_path is not None:
        console.print(f"  to restore from backup: cp {backup_path} {LOCAL_CONFIG_PATH}")


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
    source_spec: SourceSpec,
    console: Console,
) -> int:
    """``--force`` path: confirm + (optional) backup + rewrite. Returns exit code."""
    force_choice = _prompt_force_confirm(no_prompt=no_prompt)
    backup_path: Path | None = None
    match force_choice:
        case ForceChoice.ABORT:
            console.print("[red]✗ aborted[/red] — no changes")
            return 0
        case ForceChoice.OVERWRITE_WITH_BACKUP:
            backup_path = _backup_existing(console=console)
        case ForceChoice.OVERWRITE_NO_BACKUP:
            pass
        case _ as unreachable:
            assert_never(unreachable)
    probe = probe_environment()
    _apply_bootstrap(probe, source_spec=source_spec, console=console, force=True)
    _print_completion_report(
        source_spec=source_spec, backup_path=backup_path, console=console
    )
    return 0


def _handle_fresh_init(
    *,
    no_prompt: bool,
    source_spec: SourceSpec,
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
    _apply_bootstrap(probe, source_spec=source_spec, console=console)
    _print_completion_report(source_spec=source_spec, console=console)
    return 0


@app.command(epilog=INIT_EXAMPLES)
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
    config_repo: bool = typer.Option(
        False,
        "--config-repo",
        help=(
            "Also scaffold a config-repo (git init + starter setforge.yaml + "
            "tracked/) and wire it as the source. Idempotent."
        ),
    ),
) -> None:
    """Bootstrap setforge config dirs + local.yaml template + env health.

    See mockup J in
    ``~/.claude/projects/-home-raul-setforge/specs/2026-05-18-release-blocker-workflows.md``
    for the four scenarios (fresh init / reinit / --force / --check).
    """
    console = Console(stderr=True)
    console.print("=== setforge init ===")
    if check:
        _handle_check_mode(console=console)
        return

    if config_repo:
        exit_code = _handle_config_repo(no_prompt=no_prompt, console=console)
        if exit_code != 0:
            sys.exit(exit_code)
        return

    probe = probe_environment()
    if is_initialized(probe) and not force:
        _print_idempotent_reinit_report(probe, console=console)
        return

    source_spec = _prompt_source_config(
        no_prompt=no_prompt,
        path_source=path_source,
        git_source=git_source,
        git_ref=git_ref,
    )

    if force:
        exit_code = _handle_force_mode(
            no_prompt=no_prompt,
            source_spec=source_spec,
            console=console,
        )
    else:
        exit_code = _handle_fresh_init(
            no_prompt=no_prompt,
            source_spec=source_spec,
            console=console,
        )
    if exit_code != 0:
        sys.exit(exit_code)
