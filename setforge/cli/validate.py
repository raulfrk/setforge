"""validate + fetch subcommands — config-shape checks + git-source pull.

``validate`` runs a battery of config-shape checks (schema, profile
chain, Jinja2 templates, tracked srcs, claude_plugins references) for
one profile (``--profile=NAME``) or every profile (``--all``).

``fetch`` is the git-source pull entry point: clone / fetch / dirty-gate
/ checkout-ref. For path-only sources it's a no-op.
"""

from pathlib import Path

import typer
from jinja2 import StrictUndefined, Template, TemplateSyntaxError, UndefinedError
from pydantic import ValidationError

from setforge import source as source_mod
from setforge.cli import _CONFIG_OPTION, _resolve_config_arg, app
from setforge.cli._help_examples import FETCH_EXAMPLES, VALIDATE_EXAMPLES
from setforge.compare import resolve_src
from setforge.config import (
    Config,
    ResolvedProfile,
    TrackedFile,
    load_config,
    resolve_profile,
)
from setforge.errors import SetforgeError
from setforge.paths import template_context


def _check_profile(
    cfg: Config,
    prof_name: str,
    repo_root: Path,
    failures: list[str],
) -> None:
    """Run checks 2-6 for a single profile, appending failures in-place."""
    ctx = f"profile {prof_name!r}"

    resolved = _check_profile_resolution(cfg, prof_name, ctx, failures)
    if resolved is None:
        return

    for tracked_file_name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[tracked_file_name]
        dot_ctx = f"{ctx}: tracked_file {tracked_file_name!r}"
        if not _check_jinja_templates(tracked_file, dot_ctx, failures):
            continue
        _check_tracked_srcs(tracked_file, repo_root, dot_ctx, failures)

    _check_extension_includes(cfg, prof_name, ctx, failures)
    _check_claude_plugins(cfg, prof_name, ctx, failures)
    _check_marketplaces(cfg, resolved, ctx, failures)


def _check_profile_resolution(
    cfg: Config, prof_name: str, ctx: str, failures: list[str]
) -> ResolvedProfile | None:
    """Check 2: resolve profile (covers missing profiles + cycle detection)."""
    try:
        return resolve_profile(cfg, prof_name)
    except SetforgeError as exc:
        failures.append(f"{ctx}: {exc}")
        return None


def _check_jinja_templates(
    tracked_file: TrackedFile, dot_ctx: str, failures: list[str]
) -> bool:
    """Check 3: Jinja2 dst template renders with StrictUndefined.

    Returns ``True`` when the template is OK (or absent), ``False`` when a
    syntax/undefined-variable error was recorded — caller should skip
    further checks for this tracked_file.
    """
    if not tracked_file.template:
        return True
    try:
        Template(tracked_file.dst, undefined=StrictUndefined).render(
            **template_context()
        )
    except (TemplateSyntaxError, UndefinedError) as exc:
        failures.append(f"{dot_ctx}: unrenderable dst template: {exc}")
        return False
    return True


def _check_tracked_srcs(
    tracked_file: TrackedFile, repo_root: Path, dot_ctx: str, failures: list[str]
) -> None:
    """Check 4: tracked src exists on disk."""
    src = resolve_src(tracked_file, repo_root)
    if not src.exists():
        failures.append(f"{dot_ctx}: src {tracked_file.src} does not exist")


def _check_extension_includes(
    cfg: Config, prof_name: str, ctx: str, failures: list[str]
) -> None:
    """Check 5: extension include list — non-empty IDs, no duplicates.

    Walks the raw profile (before extends-merging) so duplicates that
    ``_merge_list`` would silently drop are still caught.
    """
    raw_include = cfg.profiles[prof_name].extensions.include
    _check_dedup(
        raw_include,
        ctx=ctx,
        failures=failures,
        empty_msg="extensions.include contains empty ID",
        dup_label="extensions.include duplicate",
    )


def _check_claude_plugins(
    cfg: Config, prof_name: str, ctx: str, failures: list[str]
) -> None:
    """Check 5b: claude_plugins list — non-empty refs, no duplicates.

    Same raw-profile rationale as Check 5: ``_merge_list`` dedupes during
    ``resolve_profile``, so duplicates would be silently swallowed by the
    resolved list. Walk the raw list to catch them at config time.
    """
    raw_plugins = cfg.profiles[prof_name].claude_plugins
    _check_dedup(
        raw_plugins,
        ctx=ctx,
        failures=failures,
        empty_msg="claude_plugins contains empty ref",
        dup_label="claude_plugins duplicate",
    )


def _check_dedup(
    items: list[str],
    *,
    ctx: str,
    failures: list[str],
    empty_msg: str,
    dup_label: str,
) -> None:
    """Common dedup walk used by Check 5 and Check 5b."""
    seen: set[str] = set()
    reported_dup: set[str] = set()
    empty_reported = False
    for item in items:
        if not item.strip():
            if not empty_reported:
                failures.append(f"{ctx}: {empty_msg}")
                empty_reported = True
        elif item in seen:
            if item not in reported_dup:
                failures.append(f"{ctx}: {dup_label}: {item!r}")
                reported_dup.add(item)
        else:
            seen.add(item)


def _check_marketplaces(
    cfg: Config, resolved: ResolvedProfile, ctx: str, failures: list[str]
) -> None:
    """Check 6: claude_plugins marketplace-reference internal consistency.

    Every plugin referenced in the profile must have its marketplace
    declared in ``cfg.marketplaces``. (Plugin existence in
    ``cfg.claude_plugins`` is already validated by ``load_config`` →
    ``_validate_plugin_references``.)
    """
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


@app.command("validate", epilog=VALIDATE_EXAMPLES)
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


@app.command(epilog=FETCH_EXAMPLES)
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
