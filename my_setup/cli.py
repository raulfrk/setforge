"""Typer CLI entry point for ``my-setup``.

Commands wired in Pillar 1: ``install``, ``compare``, ``capture``, ``sync``.
Extension and Claude-plugin reconcile land in Pillars 2 and 3.
"""

import sys
from pathlib import Path

import typer

from my_setup import capture as capture_mod
from my_setup import compare as compare_mod
from my_setup import deploy
from my_setup.compare import resolve_dst, resolve_src
from my_setup.config import load_config, resolve_profile
from my_setup.errors import MySetupError

app = typer.Typer(
    help="my-setup: dotfile + extension + Claude-plugin orchestration.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


_CONFIG_OPTION = typer.Option(
    Path("my_setup.yaml"),
    "--config",
    "-c",
    help="Path to my_setup.yaml.",
    show_default=True,
)
_PROFILE_OPTION = typer.Option(
    ...,
    "--profile",
    "-p",
    help="Profile name from my_setup.yaml.",
)


@app.command()
def install(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Deploy tracked → live for every dotfile in the profile."""
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)

    deploy.bootstrap_local(resolved.bootstrap)

    for name in resolved.dotfiles:
        dotfile = cfg.dotfiles[name]
        src = resolve_src(dotfile, repo_root)
        dst = resolve_dst(dotfile)
        result = deploy.copy_atomic(
            src,
            dst,
            preserve_user_sections=dotfile.preserve_user_sections,
            preserve_user_keys=dotfile.preserve_user_keys or None,
        )
        typer.echo(f"{result.action.value:>8}  {dst}")


@app.command()
def compare(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
    full: bool = typer.Option(
        False, "--full", help="Print unified diff body for drifted entries."
    ),
    check: bool = typer.Option(
        False, "--check", help="Exit non-zero on unexpected drift (for CI)."
    ),
) -> None:
    """Report drift between tracked and live for every dotfile in the profile."""
    cfg = load_config(config)
    repo_root = config.resolve().parent
    report = compare_mod.compare_profile(cfg, profile, repo_root)

    for entry in report.entries:
        line = f"{entry.status.value:>10}  {entry.name}"
        if entry.expected_drift_keys or entry.unexpected_drift_keys:
            line += (
                f"  (expected={len(entry.expected_drift_keys)},"
                f" unexpected={len(entry.unexpected_drift_keys)})"
            )
        typer.echo(line)
        if full and entry.diff:
            typer.echo(entry.diff)

    if check and report.has_unexpected_drift:
        raise typer.Exit(code=1)


@app.command()
def capture(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Capture live → tracked for every dotfile in the profile."""
    cfg = load_config(config)
    repo_root = config.resolve().parent
    results = capture_mod.capture_profile(cfg, profile, repo_root)
    for result in results:
        typer.echo(f"{result.action.value:>8}  {result.name}")


@app.command()
def sync(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Alias for ``capture`` (live → tracked)."""
    capture(profile=profile, config=config)


def main() -> None:
    """Entry point that wraps ``app`` with :class:`MySetupError` handling."""
    try:
        app()
    except MySetupError as exc:
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        sys.exit(1)


if __name__ == "__main__":
    main()
