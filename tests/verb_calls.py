"""Call the domain verbs with the inputs the CLI computes for them."""

from collections.abc import Mapping
from pathlib import Path

from setforge import capture as capture_mod
from setforge.cli import sync as sync_cli
from setforge.cli._helpers import ProfileContext
from setforge.config import Config, ResolvedProfile, resolve_profile


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
) -> tuple[capture_mod.CapturePreview, ...]:
    """Preview a capture as ``sync`` does before asking for confirmation."""
    resolved, authorized = _capture_inputs(config, profile, repo, resolved)
    return capture_mod.preview_capture_profile(
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
