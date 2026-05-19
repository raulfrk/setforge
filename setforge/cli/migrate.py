"""``setforge migrate`` subcommand — schema migration driver.

Renders a per-migration manifest (``--check``), or stages a multi-file
diff preview + arrow-key abort/apply/apply-no-backup confirmation
(``--apply``), or writes a ``schema_version`` pin into ``setforge.yaml``
to stay on an older schema (``--pin=X.Y``).

The Migration Protocol the driver consumes covers the FULL set of
local-file changes for a single version bump — ``setforge.yaml``,
``local.yaml``, tracked content, host-local state — not just the
schema YAML. Backups, diff preview, and rollback all operate at
multi-file granularity (see :mod:`setforge.migrations` for the
Protocol definition).

In v0.2.0 :data:`setforge.migrations.MIGRATIONS` is empty: ``--check``
reports ``"no migrations available"`` and exits 0; ``--apply`` says
``"nothing to apply"`` and exits 0; ``--pin`` writes the pin
unconditionally. The first real migration ships in v0.3.0.
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer

from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.errors import ConfirmRequiresInteractive
from setforge.migrations import (
    Migration,
    MigrationRoots,
    _fs_ops,
    _yaml_ops,
    current_expected_schema_version,
    detect_current_schema,
    find_migration_path,
)

# ``prompt_toolkit.shortcuts.radiolist_dialog`` is imported lazily via
# the module-level ``__getattr__`` below — non-interactive callers and
# the cold-start path of ``setforge migrate --check`` / ``--help`` never
# pay the ~140ms cost. The TUI fires only on the ``--apply`` confirm
# path. The module-attribute access path is preserved so the test suite
# can ``monkeypatch.setattr("setforge.cli.migrate.radiolist_dialog",
# ...)`` for headless test runs.


def __getattr__(name: str) -> Any:  # noqa: ANN401 — PEP 562 module hook returns Any
    if name == "radiolist_dialog":
        from prompt_toolkit.shortcuts import radiolist_dialog

        return radiolist_dialog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["MigrateChoice", "migrate"]


class MigrateChoice(StrEnum):
    """Closed set of outcomes from the ``--apply`` confirm wizard."""

    ABORT = "abort"
    APPLY_WITH_BACKUP = "apply-with-backup"
    APPLY_NO_BACKUP = "apply-no-backup"


@app.command()
def migrate(
    check: bool = typer.Option(
        False, "--check", help="Inventory migrations needed; no mutations."
    ),
    apply_flag: bool = typer.Option(
        False,
        "--apply",
        help="Apply the migration chain after multi-file confirm.",
    ),
    pin: str | None = typer.Option(
        None,
        "--pin",
        help="Write `schema_version: <X.Y>` into setforge.yaml and exit.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the arrow-key confirm — used by non-interactive callers.",
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Run schema migrations against the active ``setforge.yaml``.

    Mutually exclusive: ``--check`` + ``--apply``. ``--pin`` short-
    circuits both. ``--yes`` collapses the arrow-key confirm to the
    ``APPLY_WITH_BACKUP`` outcome — required when stdin is not a TTY.
    """
    if check and apply_flag:
        raise typer.BadParameter("--check and --apply are mutually exclusive")
    cfg_path = _resolve_config_arg(config)
    if pin is not None:
        _write_pin(cfg_path=cfg_path, pin=pin)
        return

    current = detect_current_schema(cfg_path)
    expected = current_expected_schema_version
    chain = find_migration_path(from_v=current, to_v=expected)

    if check or not apply_flag:
        _print_check_report(
            cfg_path=cfg_path, current=current, expected=expected, chain=chain
        )
        if not check and not apply_flag:
            typer.echo("specify --check, --apply, or --pin=X.Y.")
        return

    # --apply path below.
    if not chain:
        typer.echo("nothing to apply: no migrations available for this version.")
        return

    roots = MigrationRoots(
        cfg_path=cfg_path,
        repo_root=cfg_path.resolve().parent,
        home=Path.home(),
    )
    _print_multi_file_diff_preview(chain=chain, roots=roots)
    choice = _confirm_migrate(chain=chain, roots=roots, yes=yes)
    if choice is MigrateChoice.ABORT:
        typer.echo("aborted: no migrations applied.")
        return
    _execute_chain(chain=chain, roots=roots, choice=choice)
    _run_post_apply_validate(cfg_path=cfg_path)
    _print_completion_report(cfg_path=cfg_path, chain=chain, roots=roots)


# ---------------------------------------------------------------------------
# --check report
# ---------------------------------------------------------------------------


def _print_check_report(
    *,
    cfg_path: Path,
    current: str,
    expected: str,
    chain: Sequence[Migration],
) -> None:
    """Render the ``=== schema migration check ===`` block from mockup V."""
    typer.echo("=== schema migration check ===")
    typer.echo(f"your setforge.yaml:  {cfg_path}")
    typer.echo(f"  declared schema:   {current}")
    typer.echo(f"installed setforge expects schema:   {expected}")
    if not chain:
        typer.echo("=== no migrations available ===")
        return
    typer.echo(f"=== {len(chain)} migration(s) available ===")
    roots = MigrationRoots(
        cfg_path=cfg_path,
        repo_root=cfg_path.resolve().parent,
        home=Path.home(),
    )
    for migration in chain:
        typer.echo(f"{migration.from_version} → {migration.to_version}:")
        for entry in migration.manifest(roots=roots):
            line = f"  {entry.type.value} {entry.description}"
            if entry.affected_path is not None:
                line = f"{line} ({entry.affected_path})"
            typer.echo(line)
    typer.echo("=== to apply: setforge migrate --apply ===")
    typer.echo(
        "=== to skip + pin: setforge migrate --pin=X.Y (works for 1 major version) ==="
    )


# ---------------------------------------------------------------------------
# --apply preview + confirm + execute
# ---------------------------------------------------------------------------


def _print_multi_file_diff_preview(
    *,
    chain: Sequence[Migration],
    roots: MigrationRoots,
) -> None:
    """Show a per-file git-diff-style preview across every affected path.

    Snapshots each affected file's current bytes, runs the full chain
    against an in-memory copy of the filesystem state (via a dry
    pre-run on a tmp clone), then renders ``difflib.unified_diff``
    output per file. For paths that do not exist yet (new files), the
    preview is the full post-migration content marked as additions.
    """
    typer.echo("=== preview of changes ===")
    affected = _all_affected_paths(chain=chain, roots=roots)
    if not affected:
        typer.echo("(no file changes — manifest-only migration)")
        return
    previews = _render_chain_previews(chain=chain, roots=roots, paths=affected)
    for path, before, after in previews:
        if before == after:
            continue
        typer.echo(f"--- {path}")
        typer.echo(f"+++ {path}")
        diff_lines = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            n=3,
        )
        for line in diff_lines:
            typer.echo(line.rstrip("\n"))


def _render_chain_previews(
    *,
    chain: Sequence[Migration],
    roots: MigrationRoots,
    paths: Sequence[Path],
) -> list[tuple[Path, str, str]]:
    """Return ``(path, before, after)`` triples for the diff preview.

    The "after" content is computed by running the chain inside an
    isolated tmp tree mirroring the affected paths. The user's actual
    filesystem is never mutated by the preview step.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="setforge-migrate-preview-") as tmp:
        tmp_root = Path(tmp)
        mapping: dict[Path, Path] = {}
        for original in paths:
            shadow = tmp_root / _shadow_name(original)
            if original.exists():
                shutil.copy2(original, shadow)
            mapping[original] = shadow
        shadow_roots = MigrationRoots(
            cfg_path=mapping.get(roots.cfg_path, tmp_root / "setforge.yaml"),
            repo_root=tmp_root / "repo",
            home=tmp_root / "home",
        )
        # Best-effort: re-run the chain against shadows. Migrations that
        # branch on actual user filesystem layout may not be exercisable
        # in the shadow tree — preview falls back to "no diff" for those
        # paths and the real apply step does the work.
        for migration in chain:
            with _suppress_preview_errors():
                migration.apply(roots=shadow_roots)
        triples: list[tuple[Path, str, str]] = []
        for original, shadow in mapping.items():
            before = _read_or_empty(original)
            after = _read_or_empty(shadow)
            triples.append((original, before, after))
        return triples


def _shadow_name(p: Path) -> str:
    """Map an absolute affected path to a flat shadow filename.

    The shadow tree is flat so each preview path is uniquely-named
    without recreating the user's full directory layout. We replace
    path separators with ``__`` so the original name survives in the
    shadow filename for debugging.
    """
    return p.as_posix().replace("/", "__").lstrip("_")


def _read_or_empty(p: Path) -> str:
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


class _suppress_preview_errors:
    """Context manager: swallow exceptions raised inside the preview pass.

    The preview's "after" image is a best-effort render. If a migration
    branches on filesystem layout we did not faithfully shadow, the
    actual apply still runs against the real tree — the diff just
    shows ``no change`` for the unshadowable file.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        return True  # swallow whatever the migration raised


def _all_affected_paths(
    *, chain: Sequence[Migration], roots: MigrationRoots
) -> tuple[Path, ...]:
    """Concatenate every migration's ``affected_paths``, deduplicated.

    Order: first-occurrence wins, mirroring ``_merge_list`` semantics
    elsewhere in setforge. Drives backup, preview, and post-apply
    completion reporting.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for migration in chain:
        for path in migration.affected_paths(roots=roots):
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
    return tuple(out)


def _confirm_migrate(
    *, chain: Sequence[Migration], roots: MigrationRoots, yes: bool
) -> MigrateChoice:
    """Render the 3-way arrow-key radiolist; return the user's choice.

    Short-circuits to ``APPLY_WITH_BACKUP`` when ``yes`` is set (the
    safest default the wizard would have picked). Raises
    :class:`ConfirmRequiresInteractive` when stdin is not a TTY and
    ``yes`` is not set — non-TTY callers must opt in explicitly.
    """
    if yes:
        return MigrateChoice.APPLY_WITH_BACKUP
    if not sys.stdin.isatty():
        raise ConfirmRequiresInteractive(
            "setforge migrate --apply requires --yes when stdin is not a TTY"
        )
    to_version = chain[-1].to_version
    typer.echo("=== confirm ===")
    from setforge.cli import migrate as _self  # local alias for monkeypatch path

    result = _self.radiolist_dialog(
        title="setforge migrate",
        text=_dialog_text(chain=chain, roots=roots),
        values=[
            (MigrateChoice.ABORT, "no, abort — no mutations (default — safe)"),
            (
                MigrateChoice.APPLY_WITH_BACKUP,
                f"yes, apply + write per-file backups (.pre-{to_version}.bak)",
            ),
            (MigrateChoice.APPLY_NO_BACKUP, "yes, apply, no backups"),
        ],
        default=MigrateChoice.ABORT,
    ).run()
    if result is None:  # Esc — explicit user cancel
        return MigrateChoice.ABORT
    assert isinstance(result, MigrateChoice)
    return result


def _dialog_text(*, chain: Sequence[Migration], roots: MigrationRoots) -> str:
    """Compose the radiolist body text — versions + affected file count."""
    affected = _all_affected_paths(chain=chain, roots=roots)
    versions = " → ".join(
        [chain[0].from_version, *(migration.to_version for migration in chain)]
    )
    return (
        f"chain: {versions}\n"
        f"{len(chain)} migration(s); "
        f"{len(affected)} file(s) will be touched.\n\n"
        "Pick an outcome below (arrow keys + Enter; Esc to abort)."
    )


def _execute_chain(
    *,
    chain: Sequence[Migration],
    roots: MigrationRoots,
    choice: MigrateChoice,
) -> None:
    """Backup every affected file (if requested), then ``apply()`` the chain."""
    typer.echo("=== applying ===")
    if choice is MigrateChoice.APPLY_WITH_BACKUP:
        for affected in _all_affected_paths(chain=chain, roots=roots):
            if not affected.exists():
                continue
            backup = _fs_ops.backup_path(affected, chain[-1].to_version)
            shutil.copy2(affected, backup)
            typer.echo(f"  backup:  {affected.name} → {backup.name}")
    for migration in chain:
        migration.apply(roots=roots)
        typer.echo(f"  applied: {migration.from_version} → {migration.to_version}")


def _run_post_apply_validate(*, cfg_path: Path) -> None:
    """Re-run ``setforge validate --all`` to catch migration bugs.

    Shells out via ``subprocess`` so the post-apply check exercises the
    exact same CLI path users invoke manually. A non-zero exit is
    surfaced as a warning, not a hard raise — the user already has
    backups (when they opted into APPLY_WITH_BACKUP) and the rollback
    instructions printed by :func:`_print_completion_report`.
    """
    typer.echo("=== running validate post-migration ===")
    setforge_bin = shutil.which("setforge")
    if setforge_bin is None:
        typer.echo("  (skipped: `setforge` binary not on PATH)")
        return
    cmd = [setforge_bin, "validate", "--all", f"--config={cfg_path}"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode == 0:
        typer.echo("  ✓ schema parsed cleanly")
    else:
        typer.echo("  ✗ validate reported an issue:")
        for line in result.stdout.splitlines():
            typer.echo(f"    {line}")
        for line in result.stderr.splitlines():
            typer.echo(f"    {line}")


def _print_completion_report(
    *,
    cfg_path: Path,
    chain: Sequence[Migration],
    roots: MigrationRoots,
) -> None:
    """Print the closing ``=== migration complete ===`` block + rollback hint."""
    typer.echo("=== migration complete ===")
    typer.echo(f"  next: cd {roots.repo_root} && git diff {cfg_path.name}")
    to_version = chain[-1].to_version
    affected = _all_affected_paths(chain=chain, roots=roots)
    if affected:
        typer.echo(f"  to undo: restore each <file>.pre-{to_version}.bak sibling, e.g.")
        first = affected[0]
        backup = _fs_ops.backup_path(first, to_version)
        typer.echo(f"           mv {backup} {first}")


# ---------------------------------------------------------------------------
# --pin
# ---------------------------------------------------------------------------


def _write_pin(*, cfg_path: Path, pin: str) -> None:
    """Write ``schema_version: <pin>`` into ``setforge.yaml`` round-trip.

    Uses :func:`_yaml_ops.atomic_write_yaml` so a crash mid-write
    leaves the original file intact. The key is inserted at the top of
    the document when absent (immediately after ``version:`` when that
    exists), or its value is overwritten in place when already present.
    """
    if not cfg_path.exists():
        typer.echo(f"error: setforge.yaml not found at {cfg_path}", err=True)
        raise typer.Exit(1)
    yaml = _yaml_ops.yaml_rt()
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        typer.echo(f"error: setforge.yaml is empty: {cfg_path}", err=True)
        raise typer.Exit(1)
    data["schema_version"] = pin
    _yaml_ops.atomic_write_yaml(cfg_path, data)
    typer.echo(f"pinned schema_version={pin} in {cfg_path}")
