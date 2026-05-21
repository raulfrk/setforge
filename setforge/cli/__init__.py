"""Typer CLI entry point for ``setforge``.

Holds the package-level ``app = typer.Typer(...)``, the shared option
constants (``_CONFIG_OPTION``, ``_PROFILE_OPTION``, ``_SOURCE_OPTION``),
the ``--config`` source-layer resolver, the ``_root`` callback, and
``main()``. The per-subcommand bodies live in dedicated modules under
this package — see the bottom of the file for the side-effect import
block that wires each ``@app.command()`` registration.
"""

import logging
import os
import shutil
import sys
from pathlib import Path

import typer

from setforge import binaries
from setforge import source as source_mod
from setforge._log_filter import RedactingFilter
from setforge.cli._output import OutputContext, OutputFormat
from setforge.errors import SetforgeError

LOGGER: logging.Logger = logging.getLogger(__name__)

# Shared ``typer.Typer(...)`` kwargs spread onto every sub-Typer
# constructor under ``setforge/cli/``. ``rich_markup_mode=None``
# disables Rich-rendered --help so the Click ``\b`` epilog idiom
# preserves newlines AND so CliRunner substring asserts on flag
# names (e.g. ``'--dry-run' in result.stdout``) survive without ANSI
# injection breaking the match. The mode does NOT propagate from the
# root Typer to sub-Typers, so each sub-Typer must spread these
# kwargs; centralising them here means future sub-Typer additions
# automatically inherit the right rendering mode.
_TYPER_KWARGS: dict[str, object] = {"rich_markup_mode": None}


app: typer.Typer = typer.Typer(
    help="setforge: tracked file + extension + Claude plugin orchestration.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    # Cap help-output column width at 100 so CliRunner snapshot
    # assertions stay byte-for-byte stable (CliRunner pins COLUMNS=100,
    # so min(100, 100) = 100; Click #2253). Soften unconditional 100
    # for real terminals narrower than 100 cols (e.g. an 80-col SSH
    # session): degrade gracefully to the actual terminal width so the
    # help text wraps at the user's column limit instead of overflowing.
    context_settings={
        "max_content_width": 100,
        "terminal_width": min(100, shutil.get_terminal_size().columns),
    },
    **_TYPER_KWARGS,
)


_CONFIG_OPTION = typer.Option(
    Path("setforge.yaml"),
    "--config",
    "-c",
    help="Path to setforge.yaml.",
    show_default=True,
)
_PROFILE_OPTION = typer.Option(
    ...,
    "--profile",
    "-p",
    help="Profile name from setforge.yaml.",
)
_SOURCE_OPTION = typer.Option(
    None,
    source_mod.CLI_FLAG,
    help="Path to a config source directory (containing setforge.yaml). "
    "Takes precedence over SETFORGE_SOURCE and "
    "~/.config/setforge/local.yaml `source:` block. Paths only — git "
    "sources live in local.yaml. The per-command --config flag, when "
    "set explicitly, overrides this; the source-layer discovery only "
    "fires when --config is left at its default AND the CWD has no "
    "setforge.yaml.",
)


def _resolve_config_arg(config: Path) -> Path:
    """Resolve a command's ``--config`` arg through the source-layer fallback.

    Precedence:

    1. ``--config`` explicitly set (non-default) → use it (legacy flow).
    2. ``--config`` at its default → consult the source-layer (``--source``
       > ``SETFORGE_SOURCE`` > ``~/.config/setforge/local.yaml`` > CWD
       fallback), then return the ``setforge.yaml`` inside the resolved
       source dir.

    The 4th tier of the source-layer (CWD-fallback) preserves the legacy
    "run from inside config repo" UX bit-for-bit: when no source layer
    is configured, ``setforge install`` from a CWD containing
    ``setforge.yaml`` still works without ``--config``.
    """
    default = Path("setforge.yaml")
    if config != default:
        return config
    resolved_source = source_mod.get_resolved_source()
    return source_mod.validate_source_dir(resolved_source)


def _version_callback(value: bool) -> None:
    """``--version`` flag handler — print the package version and exit."""
    if value:
        from setforge import __version__

        typer.echo(__version__)
        raise typer.Exit()


def _resolve_level(verbose: int, quiet: bool) -> int:
    """Resolve the effective root-logger level from flags + env.

    Precedence (highest first):

    1. ``--quiet`` → ``ERROR``.
    2. ``-vv`` (count ≥ 2) → ``DEBUG``.
    3. ``-v`` (count == 1) → ``INFO``.
    4. ``SETFORGE_LOG_LEVEL`` env (when no -v/-q) → the named level;
       garbage values fall back to ``WARNING`` with a stderr warning.
    5. Default → ``WARNING``.

    The env-precedence rule (flag > env > default) is the existing
    behaviour from the bool ``--verbose`` path; this function carries
    it forward unchanged so backward-compat callers keep working.
    """
    if quiet:
        return logging.ERROR
    if verbose >= 2:
        return logging.DEBUG
    if verbose >= 1:
        return logging.INFO
    env_value = os.environ.get("SETFORGE_LOG_LEVEL", "WARNING")
    # `getattr(logging, env_value.upper(), None)` looks up a module
    # attribute by name; the logging module exposes both int level
    # constants (e.g. ``logging.DEBUG``) AND non-level attributes
    # (e.g. the ``Logger`` class). Restrict to ``int`` so a non-level
    # attribute or a typo falls back to WARNING instead of crashing
    # inside ``basicConfig``.
    resolved = getattr(logging, env_value.upper(), None)
    if not isinstance(resolved, int):
        sys.stderr.write(
            f"setforge: unknown SETFORGE_LOG_LEVEL={env_value!r}; "
            f"defaulting to WARNING\n"
        )
        return logging.WARNING
    return resolved


@app.callback()
def _root(
    ctx: typer.Context,
    code_bin: str | None = typer.Option(
        None,
        "--code-bin",
        help="Override path to the 'code' (VSCode) binary. "
        "Takes precedence over SETFORGE_CODE_BIN and ~/.config/setforge/local.yaml.",
    ),
    claude_bin: str | None = typer.Option(
        None,
        "--claude-bin",
        help="Override path to the 'claude' binary. "
        "Takes precedence over SETFORGE_CLAUDE_BIN and ~/.config/setforge/local.yaml.",
    ),
    gitleaks_bin: str | None = typer.Option(
        None,
        "--gitleaks-bin",
        help="Override path to the 'gitleaks' binary. "
        "Takes precedence over SETFORGE_GITLEAKS_BIN and "
        "~/.config/setforge/local.yaml.",
    ),
    patch_bin: str | None = typer.Option(
        None,
        "--patch-bin",
        help="Override path to the GNU 'patch' binary. "
        "Takes precedence over SETFORGE_PATCH_BIN and ~/.config/setforge/local.yaml.",
    ),
    source: Path | None = _SOURCE_OPTION,
    verbose: int = typer.Option(
        0,
        "--verbose",
        "-v",
        count=True,
        help="Increase verbosity: -v → INFO, -vv → DEBUG (with secret redaction).",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress non-error output (cron/CI use). Errors still emit on stderr.",
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.HUMAN,
        "--format",
        "-o",
        help="Output rendering: 'human' (default) or 'json' (versioned envelope).",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        help="Print the setforge version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Wire host-local binary overrides + source-layer override + logging + local stub.

    Side effects (in order): mutex-check ``--quiet`` + ``-v``, resolve
    effective log level, install :class:`RedactingFilter` on the root
    logger's stderr handler, set CLI binary overrides via
    ``binaries.set_cli_overrides``, ensure ``~/.config/setforge/local.yaml``
    stub exists via ``binaries.ensure_local_config_stub``, set the
    ``--source`` override via ``source_mod.set_cli_source``, wire
    :class:`OutputContext` onto ``ctx.obj`` for the renderer boundary.
    ``--version`` is wired as an eager callback that prints
    ``setforge.__version__`` and exits before this body runs.
    """
    if quiet and verbose:
        typer.echo("--quiet and --verbose/-v are mutually exclusive", err=True)
        raise typer.Exit(code=2)
    level = _resolve_level(verbose, quiet)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        force=True,
    )
    # Attach RedactingFilter to the root logger's handler (the one
    # basicConfig just installed on sys.stderr). Filters live on the
    # logger or handler that ACTUALLY emits a record — a filter on a
    # namespace-parent logger (e.g. `setforge`) is bypassed during
    # propagation up to root unless that parent also owns the handler.
    # We attach to the handler itself so EVERY record formatted through
    # this stderr handler — setforge.* and any third-party noise that
    # happens to log a setforge-shaped token — gets redacted before
    # emission.
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
    LOGGER.debug("logging configured at level %s", logging.getLevelName(level))
    # Emit a `-vv` debug breadcrumb that mentions credential-bearing env
    # vars by value. The RedactingFilter above rewrites the value before
    # the record reaches the handler, so the literal token never lands
    # on stderr — but the breadcrumb stays useful for diagnosing "why
    # did setforge see no GitHub token here". The f-string (instead of
    # `%s` lazy-format) is deliberate: the filter only rewrites
    # already-interpolated `record.msg`. The test suite asserts the
    # redaction end-to-end via this exact log site (see
    # tests/cli/test_output_modes.py:test_redacts_token_env).
    github_token = os.environ.get("SETFORGE_GITHUB_TOKEN")
    if github_token:
        LOGGER.debug(f"env credential: SETFORGE_GITHUB_TOKEN={github_token}")
    binaries.set_cli_overrides(
        code=code_bin,
        claude=claude_bin,
        gitleaks=gitleaks_bin,
        patch=patch_bin,
    )
    binaries.ensure_local_config_stub()
    source_mod.set_cli_source(source)
    ctx.obj = OutputContext(format=output_format)


# Subcommand modules — imported for the side effect of @app.command()
# registration. Must run AFTER `app`, the shared option constants, and
# `_resolve_config_arg` are defined above. Order MATTERS: it determines
# the listing order in `setforge --help` (Typer preserves registration
# order). Match the pre-split cli.py source-order so the help output
# stays bit-for-bit identical.
# isort: off
from setforge.cli import install as _install  # noqa: E402, F401
from setforge.cli import compare as _compare  # noqa: E402, F401
from setforge.cli import orphans as _orphans  # noqa: E402, F401 (cleanup-orphans — o3h8)
from setforge.cli import sync as _sync  # noqa: E402, F401 (capture+merge+sync)
from setforge.cli import revert as _revert  # noqa: E402, F401 (revert + transitions subgroup)
from setforge.cli import ext as _ext  # noqa: E402, F401
from setforge.cli import plugins as _plugins  # noqa: E402, F401 (plugin + marketplace subgroups)
from setforge.cli import validate as _validate  # noqa: E402, F401 (validate + fetch)
from setforge.cli import section as _section  # noqa: E402, F401 (section subgroup)
from setforge.cli import init as _init  # noqa: E402, F401
from setforge.cli import upgrade as _upgrade  # noqa: E402, F401
from setforge.cli import migrate as _migrate  # noqa: E402, F401 (schema migration)
from setforge.cli import status as _status  # noqa: E402, F401  (xra8)
from setforge.cli import profile as _profile  # noqa: E402, F401 (profile subgroup)
from setforge.cli import snapshot as _snapshot  # noqa: E402, F401 (of3a)
from setforge.cli import completion as _completion  # noqa: E402, F401
from setforge.cli import config as _config  # noqa: E402, F401 (setforge-7dav)
from setforge.cli import promote as _promote  # noqa: E402, F401 (setforge-dg2a)
# isort: on


def main() -> None:
    """Entry point that wraps ``app`` with :class:`SetforgeError` handling."""
    try:
        app()
    except SetforgeError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        sys.exit(1)
