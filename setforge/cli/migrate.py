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

When :data:`setforge.migrations.MIGRATIONS` resolves no chain for the
current ``schema_version`` (e.g. a config already at the expected
version), ``--check`` reports ``"no migrations available"`` and exits 0
and ``--apply`` says ``"nothing to apply"`` and exits 0; ``--pin`` writes
the pin unconditionally. The registry holds the first real migration as
of v0.3.0 (the 1.0->1.1 schema-version expand).
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import MutableMapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import typer

from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import MIGRATE_EXAMPLES
from setforge.errors import ConfirmRequiresInteractive
from setforge.migrations import (
    MIGRATIONS,
    Migration,
    MigrationRoots,
    _fs_ops,
    _yaml_ops,
    current_expected_schema_version,
    detect_current_schema,
    find_migration_path,
    known_versions,
    parse_schema_version,
)

# Strict anchored version-token: digits and dots only (e.g. ``1.0``,
# ``2.10.3``). Rejects whitespace, newlines, YAML metacharacters, and
# any other payload before a ``--pin`` value can reach ``setforge.yaml``.
_PIN_VERSION_RE: Final = re.compile(r"^[0-9]+(\.[0-9]+)*$")

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


@app.command(epilog=MIGRATE_EXAMPLES)
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
    to: str | None = typer.Option(
        None,
        "--to",
        help="Target schema version (up OR down). Default: the version "
        "this setforge expects. Mutually exclusive with --pin.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the arrow-key confirm — used by non-interactive callers.",
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Run schema migrations against the active ``setforge.yaml``.

    Mutually exclusive: ``--check`` + ``--apply``, and ``--pin`` +
    ``--to``. ``--pin`` short-circuits both check/apply. ``--to=X.Y``
    targets a specific version (up OR down); without it the target is
    :data:`current_expected_schema_version`. ``--yes`` collapses the
    arrow-key confirm to ``APPLY_WITH_BACKUP`` — required when stdin is
    not a TTY (and the only non-interactive route through a downgrade).

    Thin router: each branch delegates to a ``_dispatch_*`` helper so
    the Typer-decorated entry point stays focused on flag-shape and
    mutual-exclusion handling.
    """
    if check and apply_flag:
        raise typer.BadParameter("--check and --apply are mutually exclusive")
    if pin is not None and to is not None:
        raise typer.BadParameter("--pin and --to are mutually exclusive")
    cfg_path = _resolve_config_arg(config)
    if pin is not None:
        _dispatch_pin(cfg_path=cfg_path, pin=pin)
        return

    current = detect_current_schema(cfg_path)
    target = _resolve_target(to=to)

    # --to == current is a no-op (distinct from "no migration path"):
    # report it and exit 0 without touching the file.
    if to is not None and parse_schema_version(current) == parse_schema_version(target):
        typer.echo(f"already at schema_version {target}; nothing to do.")
        return

    chain = find_migration_path(from_v=current, to_v=target)

    if check or not apply_flag:
        _dispatch_check(
            cfg_path=cfg_path,
            current=current,
            expected=target,
            chain=chain,
            bare=not check and not apply_flag,
        )
        return

    _dispatch_apply(cfg_path=cfg_path, chain=chain, yes=yes)


def _resolve_target(*, to: str | None) -> str:
    """Resolve the migration target version, validating an explicit ``--to``.

    Without ``--to`` the target is the build's
    :data:`current_expected_schema_version`. An explicit ``--to`` must be
    a KNOWN version (:func:`known_versions`) — an unknown target raises
    :class:`typer.BadParameter` BEFORE any walk, so it can never fall
    through a string-range "reachable" check.
    """
    if to is None:
        return current_expected_schema_version
    if to not in known_versions():
        known = ", ".join(sorted(known_versions()))
        raise typer.BadParameter(
            f"unknown schema version {to!r}; known versions: {known}",
            param_hint="--to",
        )
    return to


def _dispatch_pin(*, cfg_path: Path, pin: str) -> None:
    """Handle the ``--pin=X.Y`` branch.

    Delegates to :func:`_write_pin`, which performs the round-trip
    YAML edit. Kept as a thin wrapper so the top-level router has a
    uniform ``_dispatch_*`` shape across the three branches.
    """
    _write_pin(cfg_path=cfg_path, pin=pin)


def _dispatch_check(
    *,
    cfg_path: Path,
    current: str,
    expected: str,
    chain: Sequence[Migration],
    bare: bool,
) -> None:
    """Handle the ``--check`` (and bare-invocation) branch.

    Prints the inventory report and, when invoked without any of
    ``--check`` / ``--apply`` / ``--pin``, appends the ``specify ...``
    hint so the user knows which flag to add next.
    """
    _print_check_report(
        cfg_path=cfg_path, current=current, expected=expected, chain=chain
    )
    if bare:
        typer.echo("specify --check, --apply, or --pin=X.Y.")


def _dispatch_apply(*, cfg_path: Path, chain: Sequence[Migration], yes: bool) -> None:
    """Handle the ``--apply`` branch.

    Short-circuits with ``"nothing to apply"`` when the chain is
    empty, otherwise stages the preview / confirm / execute /
    post-apply-validate sequence end-to-end.
    """
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
    _print_completion_report(cfg_path=cfg_path, chain=chain, roots=roots, choice=choice)


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
            label = f"{migration.from_version} → {migration.to_version}"
            with _suppress_preview_errors(label=label):
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
    """Context manager: swallow :class:`Exception` raised inside the preview pass.

    The preview's "after" image is a best-effort render. If a migration
    branches on filesystem layout we did not faithfully shadow, the
    actual apply still runs against the real tree — the diff just
    shows ``no change`` for the unshadowable file.

    Narrowed to :class:`Exception` so :class:`KeyboardInterrupt` and
    :class:`SystemExit` (both :class:`BaseException` subclasses) keep
    propagating: the user's Ctrl-C and Typer's ``raise typer.Exit``
    must NOT be masked by a best-effort preview pass. When the manager
    DOES suppress an exception, it emits a one-line ``preview
    unavailable`` notice so silent shadow failures surface in the
    output instead of hiding under a "no diff" rendering.
    """

    def __init__(self, *, label: str) -> None:
        self._label = label

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        if exc_type is None:
            return False
        if not issubclass(exc_type, Exception):
            return False  # let KeyboardInterrupt / SystemExit propagate
        typer.echo(f"(preview unavailable for {self._label}: {exc_type.__name__})")
        return True  # suppress Exception subclasses


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
    """Backup every affected file (if requested), then ``apply()`` the chain.

    Backup-loop posture: principled-fail. Iterate every affected path
    first, collecting any per-file backup failures into a list. If
    ANY backup fails, print each failure, abort with ``typer.Exit(1)``,
    and DO NOT call ``migration.apply()`` — better to leave the user's
    files untouched than to mutate with an incomplete safety net.
    SPEC 4 explicitly forbids shortcutting on the first failure: the
    user gets the full failure inventory in one pass.
    """
    typer.echo("=== applying ===")
    affected_paths = _all_affected_paths(chain=chain, roots=roots)
    if choice is MigrateChoice.APPLY_WITH_BACKUP:
        backup_failures: list[tuple[Path, OSError]] = []
        for affected in affected_paths:
            if not affected.exists():
                continue
            backup = _fs_ops.backup_path(affected, chain[-1].to_version)
            if backup.exists():
                # No-clobber: a backup from a prior run holds the pristine
                # pre-migration bytes. Overwriting it with the (possibly
                # already-migrated) current content would destroy the only
                # clean copy — keep the existing one.
                typer.echo(f"  backup kept (prior exists): {backup.name}")
                continue
            try:
                shutil.copy2(affected, backup)
            except OSError as exc:
                backup_failures.append((affected, exc))
                typer.secho(
                    f"  backup FAILED: {affected} — {exc}",
                    err=True,
                    fg=typer.colors.RED,
                )
                continue
            typer.echo(f"  backup:  {affected.name} → {backup.name}")
        if backup_failures:
            typer.secho(
                f"aborting migration — {len(backup_failures)} backup(s) failed",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)
    # Snapshot every affected file BEFORE mutating so a mid-chain failure
    # rolls back to a consistent pre-migration state — never a
    # half-migrated file at a schema_version inconsistent with its
    # content. Independent of the backup choice: APPLY_NO_BACKUP still
    # gets crash-consistency across a multi-step chain.
    snapshots: dict[Path, bytes | None] = {
        path: (path.read_bytes() if path.exists() else None) for path in affected_paths
    }
    applied: list[str] = []
    for migration in chain:
        step = f"{migration.from_version} → {migration.to_version}"
        try:
            migration.apply(roots=roots)
        except Exception as exc:
            _rollback(snapshots)
            typer.secho(
                f"migration step {step} failed after "
                f"{len(applied)} completed step(s) "
                f"({', '.join(applied) or 'none'}); "
                f"rolled back to the pre-migration state: {exc}",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1) from exc
        applied.append(step)
        typer.echo(f"  applied: {step}")


def _rollback(snapshots: dict[Path, bytes | None]) -> None:
    """Restore each affected path to its pre-migration snapshot.

    ``None`` marks a path that did not exist before the migration — it is
    removed if a partial apply created it. Best-effort recovery path:
    direct writes (not atomic), since the goal is to undo a failed
    multi-step apply rather than to survive a crash mid-rollback.
    """
    for path, original in snapshots.items():
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)


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
    choice: MigrateChoice,
) -> None:
    """Print the closing ``=== migration complete ===`` block + rollback hint.

    The rollback hint is suppressed when ``choice`` is
    :attr:`MigrateChoice.APPLY_NO_BACKUP`, since no ``.pre-X.Y.bak``
    siblings were written in that case. The ``choice`` flag is the sole
    source of truth — backup existence is never probed on disk.
    """
    typer.echo("=== migration complete ===")
    typer.echo(f"  next: cd {roots.repo_root} && git diff {cfg_path.name}")
    if choice is not MigrateChoice.APPLY_NO_BACKUP:
        to_version = chain[-1].to_version
        affected = _all_affected_paths(chain=chain, roots=roots)
        if affected:
            typer.echo(
                f"  to undo: restore each <file>.pre-{to_version}.bak sibling, e.g."
            )
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

    Validates ``pin`` against the migrations registry BEFORE any
    filesystem read or mutation: the value must be a strict version
    token (digits + dots) AND a known schema version (the build's
    :data:`current_expected_schema_version` plus every ``from_version``
    / ``to_version`` declared in :data:`MIGRATIONS`). An invalid pin
    raises :class:`typer.BadParameter` without touching ``setforge.yaml``.
    """
    allowed_versions = (
        {current_expected_schema_version}
        | {m.from_version for m in MIGRATIONS}
        | {m.to_version for m in MIGRATIONS}
    )
    if _PIN_VERSION_RE.fullmatch(pin) is None or pin not in allowed_versions:
        known = ", ".join(sorted(allowed_versions))
        raise typer.BadParameter(
            f"unknown schema version {pin!r}; known versions: {known}",
            param_hint="--pin",
        )
    if not cfg_path.exists():
        typer.echo(f"error: setforge.yaml not found at {cfg_path}", err=True)
        raise typer.Exit(1)
    yaml = _yaml_ops.yaml_rt()
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh)
    if data is None:
        typer.echo(f"error: setforge.yaml is empty: {cfg_path}", err=True)
        raise typer.Exit(1)
    if not isinstance(data, MutableMapping):
        # A hand-edited config whose root is a YAML list or bare scalar
        # would otherwise leak an unwrapped TypeError on the assignment
        # below. Guard it like every other parse site in the migration
        # layer (a clean CLI error, not a traceback).
        typer.echo(
            f"error: setforge.yaml root must be a mapping, got "
            f"{type(data).__name__}: {cfg_path}",
            err=True,
        )
        raise typer.Exit(1)
    data["schema_version"] = pin
    _yaml_ops.atomic_write_yaml(cfg_path, data)
    typer.echo(f"pinned schema_version={pin} in {cfg_path}")
