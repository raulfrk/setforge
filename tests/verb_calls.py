"""Call the domain verbs with the inputs the CLI computes for them."""

import stat
from collections.abc import Mapping
from pathlib import Path

from setforge import capture as capture_mod
from setforge import compare as compare_mod
from setforge import deploy
from setforge.cli import sync as sync_cli
from setforge.cli._helpers import ProfileContext
from setforge.config import (
    Config,
    ResolvedProfile,
    TrackedFile,
    resolve_and_expand,
    resolve_profile,
)


def _capture_inputs(
    config: Config, profile: str, repo: Path, resolved: ResolvedProfile | None
) -> tuple[ResolvedProfile, Mapping[str, bool]]:
    if resolved is None:
        resolved = resolve_profile(config, profile)
    ctx = ProfileContext(cfg=config, resolved=resolved, repo_root=repo, profile=profile)
    _decisions, authorized = sync_cli._capture_ownership(
        ctx, sync_cli._read_capture_owner_id(repo)
    )
    return resolved, authorized


def preview_capture_profile(
    config: Config,
    profile: str,
    repo: Path,
    *,
    resolved: ResolvedProfile | None = None,
) -> tuple[capture_mod.CaptureItem, ...]:
    """Preview a capture as ``sync`` does before asking for confirmation."""
    resolved, authorized = _capture_inputs(config, profile, repo, resolved)
    return capture_mod.plan_capture(
        config, profile, repo, resolved=resolved, ownership_authorized=authorized
    )


def capture_profile(
    config: Config,
    profile: str,
    repo: Path,
    *,
    resolved: ResolvedProfile | None = None,
) -> list[capture_mod.CaptureResult]:
    """Capture live into tracked as ``sync`` does after confirmation."""
    resolved, authorized = _capture_inputs(config, profile, repo, resolved)
    return capture_mod.capture_profile(
        config, profile, repo, resolved=resolved, ownership_authorized=authorized
    )


def compare_profile(
    config: Config, profile: str, repo: Path
) -> compare_mod.CompareReport:
    """Compare tracked against live as the ``compare`` command does."""
    resolved = resolve_and_expand(config, profile, repo)
    return compare_mod.compare_profile(
        config,
        profile,
        repo,
        resolved=resolved,
        ownership_authorized=compare_mod.file_authorization_map(config, resolved, repo),
    )


def copy_atomic(
    src: Path, dst: Path, *, backup: bool = True, mode: int | None = None
) -> deploy.DeployResult:
    """Resolve then write one file, the two steps install runs per tracked file."""
    return deploy.write_resolved_deploy(
        deploy.resolve_deploy(src, dst, mode=mode), backup=backup
    )


def deploy_symlinked_file(
    src: Path, dst: Path, tracked_file: TrackedFile, *, backup: bool = True
) -> deploy.DeployResult:
    """Deploy a symlinked file from the source snapshot the install plan takes."""
    return deploy.deploy_symlinked_file(
        dst,
        tracked_file,
        source_content=src.read_bytes(),
        source_mode=(
            tracked_file.mode
            if tracked_file.mode is not None
            else stat.S_IMODE(src.stat().st_mode)
        ),
        backup=backup,
    )
