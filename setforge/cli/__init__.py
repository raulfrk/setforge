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
from setforge.config import Config, load_config, resolve_profile
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


def _check_profile(
    cfg: Config,
    prof_name: str,
    repo_root: Path,
    failures: list[str],
) -> None:
    """Run checks 2-6 for a single profile, appending failures in-place."""
    from jinja2 import StrictUndefined, Template, TemplateSyntaxError, UndefinedError

    from setforge.compare import resolve_src
    from setforge.paths import template_context

    ctx = f"profile {prof_name!r}"

    # Check 2: Profile resolution (covers missing profiles + cycle detection).
    try:
        resolved = resolve_profile(cfg, prof_name)
    except SetforgeError as exc:
        failures.append(f"{ctx}: {exc}")
        return

    for tracked_file_name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[tracked_file_name]
        dot_ctx = f"{ctx}: tracked_file {tracked_file_name!r}"

        # Check 3: Jinja2 dst template renderability (StrictUndefined catches typos).
        if tracked_file.template:
            try:
                Template(tracked_file.dst, undefined=StrictUndefined).render(
                    **template_context()
                )
            except (TemplateSyntaxError, UndefinedError) as exc:
                failures.append(f"{dot_ctx}: unrenderable dst template: {exc}")
                continue

        # Check 4: tracked src exists on disk.
        src = resolve_src(tracked_file, repo_root)
        if not src.exists():
            failures.append(f"{dot_ctx}: src {tracked_file.src} does not exist")

    # Check 5: extension include list — non-empty IDs, no duplicates.
    # Check the raw profile (before extends-merging) so duplicates that
    # _merge_list would silently drop are still caught here.
    raw_include = cfg.profiles[prof_name].extensions.include
    seen_ext: set[str] = set()
    reported_dup_ext: set[str] = set()
    empty_reported_ext = False
    for ext_id in raw_include:
        if not ext_id.strip():
            if not empty_reported_ext:
                failures.append(f"{ctx}: extensions.include contains empty ID")
                empty_reported_ext = True
        elif ext_id in seen_ext:
            if ext_id not in reported_dup_ext:
                failures.append(f"{ctx}: extensions.include duplicate: {ext_id!r}")
                reported_dup_ext.add(ext_id)
        else:
            seen_ext.add(ext_id)

    # Same raw-profile rationale as Check 5: _merge_list dedupes during
    # resolve_profile, so duplicates would be silently swallowed by the
    # resolved list. Walk the raw list to catch them at config time.
    raw_plugins = cfg.profiles[prof_name].claude_plugins
    seen_plugin: set[str] = set()
    reported_dup_plugin: set[str] = set()
    empty_reported_plugin = False
    for plugin_ref in raw_plugins:
        if not plugin_ref.strip():
            if not empty_reported_plugin:
                failures.append(f"{ctx}: claude_plugins contains empty ref")
                empty_reported_plugin = True
        elif plugin_ref in seen_plugin:
            if plugin_ref not in reported_dup_plugin:
                failures.append(f"{ctx}: claude_plugins duplicate: {plugin_ref!r}")
                reported_dup_plugin.add(plugin_ref)
        else:
            seen_plugin.add(plugin_ref)

    # Check 6: claude_plugins marketplace-reference internal consistency.
    # Every plugin referenced in the profile must have its marketplace
    # declared in cfg.marketplaces. (Plugin existence in cfg.claude_plugins
    # is already validated by load_config → _validate_plugin_references.)
    marketplace_keys = set(cfg.marketplaces)
    for plugin_ref in resolved.claude_plugins:
        bare_name = plugin_ref.split("@")[0]
        if bare_name in cfg.claude_plugins:
            mp_name = cfg.claude_plugins[bare_name].marketplace
            if mp_name not in marketplace_keys:
                failures.append(
                    f"{ctx}: plugin {bare_name!r} references unknown "
                    f"marketplace {mp_name!r}"
                )


@app.command("validate")
def validate(
    profile: str | None = typer.Option(
        None, "--profile", help="Validate a specific profile."
    ),
    all_profiles: bool = typer.Option(
        False, "--all", help="Validate every profile in the YAML."
    ),
    config: Path = _CONFIG_OPTION,
) -> None:
    """Config-shape validation; no filesystem comparison or live target paths."""
    config = _resolve_config_arg(config)
    if profile is not None and all_profiles:
        typer.secho(
            "error: --profile and --all are mutually exclusive",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)
    if profile is None and not all_profiles:
        typer.secho(
            "error: one of --profile or --all is required",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    from pydantic import ValidationError

    failures: list[str] = []

    # Check 1: Pydantic schema validation + cross-field checks in load_config.
    try:
        cfg = load_config(config)
    except (ValidationError, SetforgeError) as exc:
        typer.echo(f"schema: {exc}")
        raise typer.Exit(1) from exc

    repo_root = config.resolve().parent

    if all_profiles:
        profiles_to_check: list[str] = list(cfg.profiles)
    else:
        assert profile is not None  # guarded above; narrow for mypy
        profiles_to_check = [profile]

    for prof_name in profiles_to_check:
        _check_profile(cfg, prof_name, repo_root, failures)

    if failures:
        for line in failures:
            typer.echo(line)
        raise typer.Exit(1)

    typer.echo("ok")


@app.command()
def fetch() -> None:
    """Clone/fetch the configured git source and check out its pinned ref.

    Resolves the active source via the 4-layer precedence (CLI ``--source``
    > ``SETFORGE_SOURCE`` env > host-local ``local.yaml`` > CWD-fallback).
    For a :class:`setforge.source.PathSource` this is a no-op. For a
    :class:`setforge.source.GitSource`: (1) clone to ``clone_dest`` if
    missing; (2) fetch ``origin``; (3) verify ``tracked/`` is clean
    (refuses to clobber user edits); (4) check out the pinned ``ref``
    (branch or SHA; default ``main``). Auth delegates to the user's
    git/SSH/credential-helper config.
    """
    resolved_source = source_mod.get_resolved_source()
    msg = source_mod.fetch_source(resolved_source)
    typer.echo(msg)


# Subcommand modules — imported for the side effect of @app.command()
# registration. Must run AFTER `app`, the shared option constants, and
# `_resolve_config_arg` are defined above.
from setforge.cli import compare as _compare  # noqa: E402, F401
from setforge.cli import ext as _ext  # noqa: E402, F401
from setforge.cli import install as _install  # noqa: E402, F401
from setforge.cli import plugins as _plugins  # noqa: E402, F401
from setforge.cli import revert as _revert  # noqa: E402, F401
from setforge.cli import sync as _sync  # noqa: E402, F401


def main() -> None:
    """Entry point that wraps ``app`` with :class:`SetforgeError` handling."""
    try:
        app()
    except SetforgeError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        sys.exit(1)


if __name__ == "__main__":
    main()
