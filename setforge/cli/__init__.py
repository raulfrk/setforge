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
import sys
from pathlib import Path

import typer

from setforge import binaries
from setforge import source as source_mod
from setforge.errors import SetforgeError

LOGGER: logging.Logger = logging.getLogger(__name__)

app: typer.Typer = typer.Typer(
    help="setforge: tracked file + extension + Claude plugin orchestration.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
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


@app.callback()
def _root(
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
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Emit DEBUG-level logging to stderr.",
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

    Side effects (in order): set CLI binary overrides via
    ``binaries.set_cli_overrides``, ensure ``~/.config/setforge/local.yaml``
    stub exists via ``binaries.ensure_local_config_stub``, set the
    ``--source`` override via ``source_mod.set_cli_source``, configure
    root logging level.
    ``--version`` is wired as an eager callback that prints
    ``setforge.__version__`` and exits before this body runs.
    """
    if verbose:
        level = logging.DEBUG
    else:
        env_value = os.environ.get("SETFORGE_LOG_LEVEL", "WARNING")
        # `getattr(logging, "Logger", None)` returns the Logger class, not None;
        # restrict to known int level constants so a non-level module attribute
        # falls back to WARNING instead of crashing inside basicConfig.
        resolved = getattr(logging, env_value.upper(), None)
        if not isinstance(resolved, int):
            sys.stderr.write(
                f"setforge: unknown SETFORGE_LOG_LEVEL={env_value!r}; "
                f"defaulting to WARNING\n"
            )
            level = logging.WARNING
        else:
            level = resolved
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        force=True,
    )
    LOGGER.debug("logging configured at level %s", logging.getLevelName(level))
    binaries.set_cli_overrides(
        code=code_bin,
        claude=claude_bin,
        gitleaks=gitleaks_bin,
        patch=patch_bin,
    )
    binaries.ensure_local_config_stub()
    source_mod.set_cli_source(source)


# Subcommand modules — imported for the side effect of @app.command()
# registration. Must run AFTER `app`, the shared option constants, and
# `_resolve_config_arg` are defined above. Order MATTERS: it determines
# the listing order in `setforge --help` (Typer preserves registration
# order). Match the pre-split cli.py source-order so the help output
# stays bit-for-bit identical.
# isort: off
from setforge.cli import install as _install  # noqa: E402, F401
from setforge.cli import compare as _compare  # noqa: E402, F401
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
# isort: on


def main() -> None:
    """Entry point that wraps ``app`` with :class:`SetforgeError` handling."""
    try:
        app()
    except SetforgeError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        sys.exit(1)
