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
from typing import cast

import typer
from pydantic import ValidationError

from setforge import binaries
from setforge import source as source_mod
from setforge._log_filter import RedactingFilter
from setforge.cli._output import OutputContext, OutputFormat, wrap_json
from setforge.errors import SetforgeError

LOGGER: logging.Logger = logging.getLogger(__name__)

# main()'s handler runs after the Typer context unwinds, so it can't read ctx.obj.
_INVOCATION_FORMAT: OutputFormat = OutputFormat.HUMAN

_SUPPORTED_OUTPUT_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("compare",),
        ("config", "show"),
        ("inspect",),
        ("ownership", "history"),
        ("ownership", "list"),
        ("profile", "show"),
        ("stage",),
        ("status",),
        ("transitions", "list"),
    }
)


# ``rich_markup_mode=None`` is passed at every ``typer.Typer(...)``
# construction site under ``setforge/cli/``. It disables Rich-rendered
# --help so the Click ``\b`` epilog idiom preserves newlines AND so
# CliRunner substring asserts on flag names (e.g.
# ``'--dry-run' in result.stdout``) survive without ANSI injection
# breaking the match. The mode does NOT propagate from the root Typer
# to sub-Typers, so each sub-Typer must pass it explicitly.
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
    rich_markup_mode=None,
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


def _output_requested(ctx_obj: OutputContext | None) -> bool:
    """Return whether an invocation requested non-default success output."""
    return ctx_obj is not None and (
        ctx_obj.quiet or ctx_obj.format is OutputFormat.JSON
    )


def _output_mode_label(ctx_obj: OutputContext) -> str:
    """Return the user-facing flag spelling for a non-default output mode."""
    return "--quiet" if ctx_obj.quiet else "--format=json"


def _reset_invocation_state() -> None:
    """Clear process globals owned by one CLI invocation."""
    global _INVOCATION_FORMAT
    _INVOCATION_FORMAT = OutputFormat.HUMAN
    binaries.set_cli_overrides()
    source_mod.set_cli_source(None)


def _commit_invocation_state(ctx: typer.Context) -> None:
    """Commit parsed root options after command-specific output validation."""
    root = ctx.find_root()
    output_ctx = cast(OutputContext, root.obj)
    params = root.params
    global _INVOCATION_FORMAT
    _INVOCATION_FORMAT = output_ctx.format
    binaries.set_cli_overrides(
        code=cast(str | None, params.get("code_bin")),
        claude=cast(str | None, params.get("claude_bin")),
        gitleaks=cast(str | None, params.get("gitleaks_bin")),
        patch=cast(str | None, params.get("patch_bin")),
    )
    source_mod.set_cli_source(cast(Path | None, params.get("source")))


def _require_output_path(
    ctx_obj: OutputContext | None,
    path: tuple[str, ...],
    *,
    allow_prefix: bool = False,
) -> None:
    """Reject a non-default output mode unless ``path`` is supported."""
    if not _output_requested(ctx_obj):
        return
    assert ctx_obj is not None
    supported = path in _SUPPORTED_OUTPUT_PATHS
    if allow_prefix:
        supported = supported or any(
            candidate[: len(path)] == path for candidate in _SUPPORTED_OUTPUT_PATHS
        )
    if supported:
        return
    command = " ".join(path)
    _reset_invocation_state()
    raise typer.BadParameter(
        f"{_output_mode_label(ctx_obj)} is not supported for {command!r}"
    )


def _require_output_condition(
    ctx_obj: OutputContext | None,
    *,
    supported: bool,
    command: str,
) -> None:
    """Reject a mode whose command supports output only with specific options."""
    if not _output_requested(ctx_obj) or supported:
        return
    assert ctx_obj is not None
    _reset_invocation_state()
    raise typer.BadParameter(
        f"{_output_mode_label(ctx_obj)} is not supported for {command!r}"
    )


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
        help="Suppress success output on commands supported by --format. "
        "Errors remain on stderr.",
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.HUMAN,
        "--format",
        "-o",
        help="Output for compare, status, inspect, profile show, transitions list, "
        "ownership list/history, stage --list, and config show --effective: "
        "'human' or versioned 'json'.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        help="Print the setforge version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Wire host-local binary overrides, source override, logging, and output.

    Invocation-local state from any prior in-process call is cleared first,
    including on validation failure. After validating output-mode
    compatibility, resolve the effective log level, install
    :class:`RedactingFilter` on the root logger's stderr handler, set CLI
    binary overrides via ``binaries.set_cli_overrides``, set the ``--source``
    override via ``source_mod.set_cli_source``, and wire
    :class:`OutputContext` onto ``ctx.obj`` for the renderer boundary.
    ``--version`` is wired as an eager callback that prints
    ``setforge.__version__`` and exits before this body runs.
    """
    # Invocation-scoped overrides must never leak across repeated in-process
    # calls, including calls rejected by the validation below.  Clear the
    # previous invocation before establishing any state for this one.
    _reset_invocation_state()
    if quiet and verbose:
        typer.echo("--quiet and --verbose/-v are mutually exclusive", err=True)
        raise typer.Exit(code=2)
    if quiet and output_format is OutputFormat.JSON:
        typer.echo("--quiet and --format=json are mutually exclusive", err=True)
        raise typer.Exit(code=2)
    output_ctx = OutputContext(format=output_format, quiet=quiet)
    ctx.obj = output_ctx
    if ctx.invoked_subcommand is not None:
        _require_output_path(output_ctx, (ctx.invoked_subcommand,), allow_prefix=True)
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
    # ``stage`` supports non-default output only with ``--list``. Click may
    # reject missing leaf arguments before the command function can validate
    # that condition, so keep process globals neutral until that gate runs.
    defer_state_commit = _output_requested(output_ctx) and (
        ctx.invoked_subcommand == "stage"
    )
    if not defer_state_commit:
        _commit_invocation_state(ctx)


# Subcommand modules — imported for the side effect of @app.command()
# registration. Must run AFTER `app`, the shared option constants, and
# `_resolve_config_arg` are defined above. Order MATTERS: it determines
# the listing order in `setforge --help` (Typer preserves registration
# order). Match the pre-split cli.py source-order so the help output
# stays bit-for-bit identical.
# isort: off
from setforge.cli import install as _install  # noqa: E402, F401
from setforge.cli import compare as _compare  # noqa: E402, F401
from setforge.cli import orphans as _orphans  # noqa: E402, F401 (cleanup-orphans)
from setforge.cli import cleanup as _cleanup  # noqa: E402, F401
from setforge.cli import sync as _sync  # noqa: E402, F401 (capture+merge+sync)
from setforge.cli import revert as _revert  # noqa: E402, F401 (revert + transitions subgroup)
from setforge.cli import recover as _recover  # noqa: E402, F401
from setforge.cli import ownership as _ownership  # noqa: E402, F401
from setforge.cli import ext as _ext  # noqa: E402, F401
from setforge.cli import plugins as _plugins  # noqa: E402, F401 (plugin + marketplace subgroups)
from setforge.cli import validate as _validate  # noqa: E402, F401 (validate + fetch)
from setforge.cli import lock as _lock  # noqa: E402, F401
from setforge.cli import init as _init  # noqa: E402, F401
from setforge.cli import upgrade as _upgrade  # noqa: E402, F401
from setforge.cli import migrate as _migrate  # noqa: E402, F401 (schema migration)
from setforge.cli import status as _status  # noqa: E402, F401
from setforge.cli import profile as _profile  # noqa: E402, F401 (profile subgroup)
from setforge.cli import snapshot as _snapshot  # noqa: E402, F401
from setforge.cli import completion as _completion  # noqa: E402, F401
from setforge.cli import config as _config  # noqa: E402, F401
from setforge.cli import stage as _stage  # noqa: E402, F401 (A5 hunk staging)
from setforge.cli import inspect as _inspect  # noqa: E402, F401 (A7 3-way viewer)
from setforge.cli import project as _project  # noqa: E402, F401 (project profiles)
# isort: on


# Root-level short flags that consume a value, mirroring Click's parser so
# the argv scan below doesn't misread the value of one option as `--config`.
_VALUE_SHORT_FLAGS = frozenset("cpo")


def _short_cluster_config(token: str) -> tuple[str | None, bool]:
    """Resolve a single-dash short-flag cluster against the `-c` option.

    ``token`` is a single-dash cluster (e.g. ``-vc``, ``-cbad.yaml``). Walk it
    left to right, Click-style: a value-taking short flag consumes the rest of
    the cluster as its attached value. Returns ``(attached, wants_next)``:

    * ``attached`` — the `-c` value carried inside this cluster, or ``None``.
      An attached short value keeps its literal remainder; Click does not strip
      a leading ``=`` the way it does for the long ``--config=`` form.
    * ``wants_next`` — ``True`` when the cluster ends on a bare value flag whose
      value is the *next* argv token; the caller consumes that token as the
      config path when the flag is `-c`, or skips it otherwise so a
      `-c…`-shaped value (e.g. ``-p -cx``) isn't misread as ``--config``.
    """
    for pos, char in enumerate(token[1:], start=1):
        if char not in _VALUE_SHORT_FLAGS:
            continue
        attached = token[pos + 1 :]
        if attached:
            # Value flag with an attached value; only `-c` yields a config.
            return (attached if char == "c" else None), False
        # Bare trailing value flag — its value is the next argv token.
        return None, True
    return None, False


def _config_path_from_argv() -> Path:
    """Recover the effective ``setforge.yaml`` path for a failed invocation.

    Scans ``sys.argv`` for the per-command ``--config`` / ``-c`` flag —
    honoring Click's spaced, attached, and stacked short-flag forms — and runs
    the value through the same :func:`_resolve_config_arg` source-layer
    fallback the command bodies use, so the polished validation-error report
    anchors its snippet + ``Fix:`` line on the real file. Best-effort: on any
    resolution failure (no source configured, unreadable dir) it falls back to
    the bare default so the error still renders — the downstream renderer
    degrades gracefully to a ``(1, 1)`` placeholder when the file can't be
    re-read.
    """
    argv = sys.argv[1:]
    config = Path("setforge.yaml")
    skip_next = False
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg in ("--config", "-c") and i + 1 < len(argv):
            config = Path(argv[i + 1])
            break
        if arg.startswith("--config="):
            config = Path(arg.split("=", 1)[1])
            break
        if arg.startswith("-") and not arg.startswith("--"):
            attached, wants_next = _short_cluster_config(arg)
            if attached is not None:
                config = Path(attached)
                break
            if wants_next:
                if arg[-1] == "c" and i + 1 < len(argv):
                    config = Path(argv[i + 1])
                    break
                skip_next = True
    try:
        return _resolve_config_arg(config)
    except SetforgeError:
        return config


def _handle_config_validation_error(exc: ValidationError) -> None:
    """Render an escaped ``load_config`` ``ValidationError`` and exit non-zero.

    Reuses ``validate``'s polished ``✗ SCHEMA VALIDATION ERROR`` formatter
    (imported lazily to avoid a top-level import cycle: ``validate`` imports
    from this package). Human mode echoes the report to stderr; ``-o json``
    mode writes the versioned ``wrap_json`` error envelope (with an ``errors``
    array) to stdout. Exit code stays ``1``, mirroring the
    :class:`SetforgeError` branch — a config-validation failure blocks the run.
    """
    from setforge.cli.validate import render_setforge_yaml_validation_error

    config_path = _config_path_from_argv()
    lines = render_setforge_yaml_validation_error(config_path, exc)
    if _INVOCATION_FORMAT is OutputFormat.JSON:
        sys.stdout.write(wrap_json("error", None, errors=lines))
        sys.stdout.write("\n")
    else:
        for line in lines:
            typer.echo(line, err=True)
    sys.exit(1)


def _handle_setforge_error(exc: SetforgeError) -> None:
    """Render a domain error in the selected output format and exit non-zero."""
    message = str(exc)
    if _INVOCATION_FORMAT is OutputFormat.JSON:
        sys.stdout.write(wrap_json("error", None, errors=[message]))
        sys.stdout.write("\n")
    else:
        typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    sys.exit(1)


def main() -> None:
    """Entry point that wraps ``app`` with :class:`SetforgeError` handling."""
    global _INVOCATION_FORMAT
    _INVOCATION_FORMAT = OutputFormat.HUMAN
    try:
        app()
    except SetforgeError as exc:
        _handle_setforge_error(exc)
    except ValidationError as exc:
        _handle_config_validation_error(exc)
