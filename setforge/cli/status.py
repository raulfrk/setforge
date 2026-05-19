"""status subcommand — one-shot situational awareness for a profile.

Renders five sections per the setforge-xra8 mockup (section O):

1. config-repo — HEAD short-sha, commits since last install, commits vs ``origin/main``.
2. last install — age and transition id of the most recent install transition.
3. drift — counts of unexpected, user-section, and expected drift.
4. overlay — counts of overlay entries declared in ``~/.config/setforge/local.yaml``.
5. capabilities — three rows from :func:`setforge.cli._init_helpers.probe_environment`.

The command is informational: it returns 0 even when capabilities are
missing or the config repo is dirty. The exit code is gated only on
hard errors raised by :func:`setforge.cli._resolve_config_arg` /
:func:`setforge.config.load_config` (no source configured, malformed
YAML, unknown profile).
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import typer

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]
from ruamel.yaml.error import YAMLError  # type: ignore[import-not-found]

from setforge import compare as compare_mod
from setforge import transitions
from setforge.cli import (
    _CONFIG_OPTION,
    _PROFILE_OPTION,
    _resolve_config_arg,
    app,
)
from setforge.cli._helpers import ProfileContext
from setforge.cli._init_helpers import (
    CapabilityProbe,
    CapabilityState,
    probe_environment,
)
from setforge.compare import CompareStatus
from setforge.config import load_config, resolve_profile
from setforge.errors import InvalidTransitionRecord
from setforge.source import LOCAL_CONFIG_PATH, get_resolved_source, resolve_source_dir

_GIT_TIMEOUT_SECONDS: int = 30
_OVERLAY_KEYS: tuple[str, ...] = (
    "extensions",
    "marketplaces",
    "plugins",
    "host_local_sections",
    "preserve_user_keys",
    "tracked_files",
)


@dataclass(frozen=True, slots=True)
class _GitInfo:
    """Resolved config-repo git state for the status report.

    Fields are ``None`` when the corresponding ``git`` invocation could
    not produce a value (binary missing, not a git repo, no remote, etc.)
    — the renderer surfaces a clean placeholder for each ``None``.
    """

    head_short: str | None
    commits_since_install: int | None
    commits_since_install_reason: str | None  # placeholder text when count is None
    commits_vs_origin: int | None
    commits_vs_origin_reason: str | None


@dataclass(frozen=True, slots=True)
class _DriftCounts:
    """Aggregated drift counts for the report."""

    unexpected: int
    user_section: int
    expected: int


def _git_run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git`` with locale lockdown + non-raising exit handling.

    Returns the :class:`subprocess.CompletedProcess`. Locale is forced
    to ``C`` so non-porcelain output (``rev-parse``, ``rev-list``) stays
    parser-stable. Callers must inspect ``returncode`` and ``stdout`` to
    decide how to render failures; this helper never raises on a
    non-zero exit so status never blocks on a git glitch.
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        return subprocess.CompletedProcess(
            args=args, returncode=127, stdout="", stderr="git binary not on PATH"
        )
    env = {**os.environ, "LANG": "C", "LC_ALL": "C"}
    try:
        return subprocess.run(
            [git_bin, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            cwd=cwd,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr=str(exc)
        )


def _resolve_head_short(source_dir: Path) -> str | None:
    """Return the 7-char short sha of HEAD, or None when unavailable."""
    result = _git_run(["rev-parse", "--short=7", "HEAD"], cwd=source_dir)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _commits_since_sha(source_dir: Path, prev_sha: str) -> int | None:
    """Return ``git rev-list --count <prev_sha>..HEAD`` or None on error."""
    result = _git_run(["rev-list", "--count", f"{prev_sha}..HEAD"], cwd=source_dir)
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _commits_vs_origin_main(source_dir: Path) -> tuple[int | None, str | None]:
    """Return (count_ahead, placeholder_reason). Exactly one is non-None.

    Counts commits on HEAD that are not on ``origin/main``. When the
    remote ref is missing (or the repo is not a git repo), returns
    ``(None, "<reason>")`` so the renderer can show a friendly
    placeholder.
    """
    # First confirm origin/main exists; rev-list count "origin/main..HEAD"
    # against a missing ref would fall through with a misleading 0.
    probe = _git_run(["rev-parse", "--verify", "origin/main"], cwd=source_dir)
    if probe.returncode != 0:
        return None, "no origin/main remote"
    result = _git_run(["rev-list", "--count", "origin/main..HEAD"], cwd=source_dir)
    if result.returncode != 0:
        return None, "git error"
    raw = result.stdout.strip()
    if not raw.isdigit():
        return None, "git error"
    return int(raw), None


def _is_git_repo(source_dir: Path) -> bool:
    """Return True iff ``source_dir`` is inside a git working tree."""
    result = _git_run(["rev-parse", "--is-inside-work-tree"], cwd=source_dir)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _resolve_git_info(source_dir: Path, prev_sha: str | None) -> _GitInfo:
    """Resolve the full git block for the status report.

    Falls back gracefully when ``source_dir`` is not a git repo: every
    field is ``None`` and the renderer prints a single
    ``(config dir not a git repo)`` placeholder block.
    """
    if not _is_git_repo(source_dir):
        return _GitInfo(
            head_short=None,
            commits_since_install=None,
            commits_since_install_reason="config dir not a git repo",
            commits_vs_origin=None,
            commits_vs_origin_reason="config dir not a git repo",
        )
    head_short = _resolve_head_short(source_dir)
    if prev_sha is None:
        commits_since_install: int | None = None
        commits_since_reason: str | None = (
            "requires source_sha; this transition predates schema bump"
        )
    else:
        count = _commits_since_sha(source_dir, prev_sha)
        if count is None:
            commits_since_install = None
            commits_since_reason = "git error"
        else:
            commits_since_install = count
            commits_since_reason = None
    commits_vs_origin, commits_vs_origin_reason = _commits_vs_origin_main(source_dir)
    return _GitInfo(
        head_short=head_short,
        commits_since_install=commits_since_install,
        commits_since_install_reason=commits_since_reason,
        commits_vs_origin=commits_vs_origin,
        commits_vs_origin_reason=commits_vs_origin_reason,
    )


def _format_age(now: datetime, then: datetime) -> str:
    """Format the age of a UTC timestamp as ``Nh ago`` / ``Nd ago``."""
    delta = now - then
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    minutes, _ = divmod(seconds, 60)
    hours, _ = divmod(minutes, 60)
    days, _ = divmod(hours, 24)
    if days >= 1:
        return f"{days}d ago"
    if hours >= 1:
        return f"{hours}h ago"
    if minutes >= 1:
        return f"{minutes}m ago"
    return f"{seconds}s ago"


def _load_last_install_meta(profile: str) -> transitions.TransitionMeta | None:
    """Return the most-recent INSTALL transition meta for ``profile``, or None.

    Filters to ``TransitionCommand.INSTALL`` so a later sync or revert
    doesn't shadow the install row — the "last install:" label promises
    install-specific provenance (and the source_sha that drives
    commits-since-install) and only install transitions carry that
    payload semantics.
    """
    latest_dir = transitions.load_latest(
        profile, command=transitions.TransitionCommand.INSTALL
    )
    if latest_dir is None:
        return None
    with contextlib.suppress(InvalidTransitionRecord):
        return transitions.load_meta(latest_dir)
    return None


def _compute_drift_counts(ctx: ProfileContext) -> _DriftCounts:
    """Compute approximate drift counts for status rendering.

    Counts are split into three buckets:

    - ``user_section``: entries with section markers AND a non-empty
      diff body.
    - ``expected``: entries whose drift is fully covered by preserve
      overlays (``expected_drift_keys`` set, no diff body).
    - ``unexpected``: anything else (the fallback branch, including
      entries with ``unexpected_drift_keys`` and diff-bearing entries
      without section markers).

    This is an approximation — precise section-reconcile
    classification requires a full reconcile pass which status
    deliberately skips for cost. Use ``setforge compare`` for the
    authoritative drift state.
    """
    report = compare_mod.compare_profile(ctx.cfg, ctx.profile, ctx.repo_root)
    unexpected = 0
    user_section = 0
    expected = 0
    for entry in report.entries:
        if entry.status is not CompareStatus.DRIFTED:
            continue
        tracked_file = ctx.cfg.tracked_files.get(entry.name.split("/", 1)[0])
        section_bearing = bool(tracked_file and tracked_file.preserve_user_sections)
        if entry.unexpected_drift_keys:
            unexpected += 1
        elif entry.diff and section_bearing:
            user_section += 1
        elif entry.expected_drift_keys and not entry.diff:
            expected += 1
        else:
            unexpected += 1
    return _DriftCounts(
        unexpected=unexpected, user_section=user_section, expected=expected
    )


def _read_overlay_counts(local_yaml: Path) -> dict[str, int]:
    """Parse ``local.yaml`` overlay blocks and return a per-key entry count.

    Returns an empty dict when the file is absent or unparseable — the
    overlay section then renders as ``(no overlays)``. Top-level keys
    in :data:`_OVERLAY_KEYS` map to their entry counts (list length for
    list shapes, mapping length for dict shapes).
    """
    if not local_yaml.exists():
        return {}
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(local_yaml.read_text(encoding="utf-8"))
    except (OSError, YAMLError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    counts: dict[str, int] = {}
    for key in _OVERLAY_KEYS:
        block = data.get(key)
        if isinstance(block, list | Mapping):
            counts[key] = len(block)
    return counts


def _render_config_repo(
    *,
    source_dir: Path,
    git_info: _GitInfo,
) -> None:
    """Print the ``config-repo`` section."""
    head = git_info.head_short or "(no HEAD)"
    typer.echo(f"config-repo:    {source_dir} @ {head}")
    if git_info.commits_since_install is None:
        reason = git_info.commits_since_install_reason or "unavailable"
        typer.echo(f"                  ↳ commits-since-install: (— {reason})")
    else:
        count = git_info.commits_since_install
        tail = "(up to date)" if count == 0 else f"({count} new since last install)"
        typer.echo(
            f"                  ↳ {count} commit{'s' if count != 1 else ''} "
            f"ahead of last-installed state {tail}"
        )
    if git_info.commits_vs_origin is None:
        reason = git_info.commits_vs_origin_reason or "unavailable"
        typer.echo(f"                  ↳ vs origin/main: (— {reason})")
    else:
        count = git_info.commits_vs_origin
        tail = "(in sync)" if count == 0 else "(not pushed)"
        typer.echo(
            f"                  ↳ {count} commit{'s' if count != 1 else ''} "
            f"ahead of origin/main {tail}"
        )


def _render_last_install(
    *,
    profile: str,
    meta: transitions.TransitionMeta | None,
    now: datetime,
) -> None:
    """Print the ``last install`` section."""
    if meta is None:
        typer.echo(f"last install:   (no transitions recorded for profile {profile!r})")
        return
    age = _format_age(now, meta.timestamp.astimezone(UTC))
    dirname = transitions.transition_dirname(
        meta.timestamp, meta.command.value, meta.profile
    )
    typer.echo(f"last install:   {age} (transition {dirname})")


def _render_drift(drift: _DriftCounts) -> None:
    """Print the ``drift`` section."""
    typer.echo(
        f"drift:          {drift.unexpected} unexpected, "
        f"{drift.user_section} user-section drift, "
        f"{drift.expected} expected (preserve_user_keys)"
    )


def _render_overlay(overlay_counts: Mapping[str, int]) -> None:
    """Print the ``overlay`` section."""
    if not overlay_counts:
        typer.echo("overlay:        (no overlays in local.yaml)")
        return
    parts = [f"{key} +{count}" for key, count in overlay_counts.items() if count]
    if not parts:
        typer.echo("overlay:        (no overlays in local.yaml)")
        return
    typer.echo(f"overlay:        {', '.join(parts)}")


def _render_capability(capability: CapabilityProbe) -> str:
    """Format one capability row as ``✓ label`` or ``✗ label (reason)``."""
    mark = "✓" if capability.state is CapabilityState.ENABLED else "✗"
    if capability.state is CapabilityState.ENABLED:
        return f"{mark} {capability.label}"
    reason = capability.reason or "disabled"
    return f"{mark} {capability.label} {reason}"


def _render_capabilities(capabilities: tuple[CapabilityProbe, ...]) -> None:
    """Print the ``capabilities`` section."""
    formatted = "  ".join(_render_capability(c) for c in capabilities)
    typer.echo(f"capabilities:   {formatted}")


@app.command()
def status(
    profile: str = _PROFILE_OPTION,
    config: Path = _CONFIG_OPTION,
) -> None:
    """Print a one-screen status summary for ``profile``.

    Read-only command: exits 0 even when capabilities are missing, the
    config repo is dirty, or the git remote is unreachable. The only
    non-zero exits come from
    :func:`setforge.cli._resolve_config_arg` /
    :func:`setforge.config.load_config` /
    :func:`setforge.config.resolve_profile` failures, which surface via
    :class:`setforge.errors.SetforgeError`.
    """
    config = _resolve_config_arg(config)
    cfg = load_config(config)
    repo_root = config.resolve().parent
    resolved = resolve_profile(cfg, profile)
    ctx = ProfileContext(
        cfg=cfg, resolved=resolved, repo_root=repo_root, profile=profile
    )
    source = get_resolved_source()
    source_dir = resolve_source_dir(source)
    host = platform.node() or "unknown-host"

    typer.echo(f"=== setforge status — {profile} on {host} ===")

    meta = _load_last_install_meta(profile)
    git_info = _resolve_git_info(
        source_dir, meta.source_sha if meta is not None else None
    )
    _render_config_repo(source_dir=source_dir, git_info=git_info)

    now = datetime.now(UTC)
    _render_last_install(profile=profile, meta=meta, now=now)

    drift = _compute_drift_counts(ctx)
    _render_drift(drift)

    overlay_counts = _read_overlay_counts(LOCAL_CONFIG_PATH)
    _render_overlay(overlay_counts)

    probe = probe_environment(prev_state=None)
    _render_capabilities(probe.capabilities)

    typer.echo("=== ready: run install if any drift surfaces or after fetch ===")
