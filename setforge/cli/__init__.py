"""Typer CLI entry point for ``setforge``.

Commands wired in Pillar 1: ``install``, ``compare``, ``capture``, ``sync``.
Pillar 2 adds extension reconcile inside ``install``. Claude plugin
reconcile lands in Pillar 3. ``revert`` (setforge-19n) replays the most
recent transition for a profile in reverse.
"""

import logging
import os
import sys
from pathlib import Path

import typer

from setforge import binaries
from setforge import source as source_mod
from setforge.errors import SetforgeError

LOGGER = logging.getLogger(__name__)

app = typer.Typer(
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
) -> None:
    """Wire host-local binary overrides, configure logging, ensure local stub exists."""
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
    binaries.set_cli_overrides(code=code_bin, claude=claude_bin, patch=patch_bin)
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
# isort: on


def main() -> None:
    """Entry point that wraps ``app`` with :class:`SetforgeError` handling."""
    try:
        app()
    except SetforgeError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        sys.exit(1)
