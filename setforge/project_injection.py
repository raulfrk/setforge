"""Reversible project-profile injection into one verified Git worktree."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from setforge import atomicio, operations
from setforge.config import (
    ProjectVisibility,
    ResolvedProjectFile,
    ResolvedProjectProfile,
)
from setforge.errors import SetforgeError
from setforge.file_ownership import file_resource_id, refuse_active_file_claims
from setforge.git_info import run_git
from setforge.git_overlay import (
    OverlayClaim,
    OverlayGitPlan,
    apply_overlay_git,
    overlay_claim_id,
    plan_overlay_git,
    read_overlay_claims,
)
from setforge.git_visibility import (
    VisibilityClaim,
    VisibilityPlan,
    apply_claims,
    claim_id,
    info_exclude_path,
    plan_claims,
    read_claims,
)
from setforge.locking import MutationLockGuards, TargetLockGuard, mutation_locks
from setforge.orphan_scan import capture_parent_path_guards
from setforge.ownership import (
    Authority,
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
    ResourceScope,
    ScopeKind,
    load_or_create_owner_id_locked,
    read_owner_id,
    read_owner_id_locked,
    resolve_owner_common_dir,
)
from setforge.paths import state_root
from setforge.project_overlay import (
    ProjectOverlay,
    build_overlay,
    clean_content,
    overlay_path,
    read_overlay,
    write_overlay,
)
from setforge.project_record import (
    _LEGACY_MANIFEST_SCHEMA,
    _MANIFEST_SCHEMA,
    _PRIOR_MANIFEST_SCHEMA,
    ProjectFileAction,
    _record_files,
    _sha256,
)

if TYPE_CHECKING:
    from setforge.project_sync import AutoResolution


@dataclass(frozen=True, slots=True)
class ProjectFilePlan:
    """Immutable source, destination, and exact pre-state for one file."""

    file_id: str
    declaring_profile: str
    source: Path
    destination: Path
    relative_destination: Path
    source_payload: bytes
    source_mode: int
    source_digest: str
    applied_payload: bytes | None
    action: ProjectFileAction
    previous_payload: bytes | None
    previous_mode: int | None
    created_parents: tuple[Path, ...]
    overlay: ProjectOverlay | None = None


@dataclass(frozen=True, slots=True)
class ProjectInjectionPlan:
    """A complete no-write inject plan bound to current filesystem state."""

    profile: str
    target: Path
    target_device: int
    target_inode: int
    git_dir: Path | None
    visibility: ProjectVisibility
    config_root: Path
    config_path: Path
    files: tuple[ProjectFilePlan, ...]
    manifest_path: Path
    visibility_plan: VisibilityPlan | None
    overlay_git_plan: OverlayGitPlan | None


@dataclass(frozen=True, slots=True)
class ProjectRemovePlan:
    """A drift-validated removal plan backed by one durable manifest."""

    profile: str
    target: Path
    target_device: int
    target_inode: int
    config_path: Path
    manifest_path: Path
    owner_id: uuid.UUID
    files: tuple[ProjectFilePlan, ...]
    created_parents: tuple[Path, ...]
    visibility_plan: VisibilityPlan | None
    overlay_git_plan: OverlayGitPlan | None
    #: Missing tracked-overlay destinations that Git's index no longer has.
    git_removed: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectStaleRemovalPlan:
    """Private state left behind by an injection that can no longer be removed."""

    profile: str
    target: Path
    reason: str
    config_path: Path
    manifest_path: Path | None
    owner_id: uuid.UUID
    claims: tuple[OwnershipClaim, ...]
    overlay_paths: tuple[Path, ...]
    visibility_plan: VisibilityPlan | None
    overlay_git_plan: OverlayGitPlan | None


def identity_remedy(
    raw: dict[str, object], target: Path, inode: int, git_dir: Path | None
) -> str | None:
    """Say how a record differs from its live directory and what resolves it.

    A replaced directory can only have its record dropped. A changed Git
    directory in the same directory still allows a normal removal.
    """
    remove = f"`setforge project remove {raw['profile']} {target}`"
    if raw["target_inode"] != inode:
        return f"run {remove} to drop the stale record"
    live = str(git_dir) if git_dir is not None else None
    if raw["git_dir"] != live:
        return (
            "the Git directory changed since injection (recorded "
            f"{raw['git_dir'] or 'none'}, now {live or 'none'}); run {remove} to "
            "remove the injection, then inject again"
        )
    return None


def missing_file_remedy(target: Path, profile: object, action: object) -> str:
    """Name the commands that settle a recorded file missing from the project.

    Sync cannot restore a member overlaid on a file Git tracks.
    """
    if action == ProjectFileAction.OVERLAY:
        return (
            f"run `setforge project remove {profile} {target}` to remove the "
            "injection, or restore the file with Git"
        )
    return (
        f"run `setforge project sync {target} --auto=use-profile` to restore it, "
        f"or `setforge project sync {target}` to keep it deleted"
    )


def _injection_key(target: Path, profile: str) -> str:
    payload = json.dumps(
        {"profile": profile, "target": str(target)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def manifest_path(target: Path, profile: str) -> Path:
    """Return the private durable record for one target/profile pair."""
    return (
        state_root() / "project-injections" / f"{_injection_key(target, profile)}.json"
    )


def _verified_git_worktree(path: Path) -> tuple[Path, Path, os.stat_result]:
    lexical = path.expanduser().absolute()
    try:
        resolved = lexical.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise SetforgeError(
            f"project target cannot be resolved: {lexical}: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise SetforgeError(f"project target is not a directory: {resolved}")
    try:
        top = Path(
            run_git(resolved, ["rev-parse", "--show-toplevel"]).stdout.strip()
        ).resolve()
        git_dir_raw = run_git(resolved, ["rev-parse", "--git-dir"]).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetforgeError(
            f"project target must be an existing Git worktree root: {resolved}"
        ) from exc
    if top != resolved:
        raise SetforgeError(
            f"project target must be the Git worktree root {top}, not {resolved}"
        )
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        git_dir = resolved / git_dir
    try:
        git_dir = git_dir.resolve(strict=True)
        target_stat = resolved.stat()
    except OSError as exc:
        raise SetforgeError(
            f"project target identity cannot be read: {resolved}: {exc}"
        ) from exc
    return resolved, git_dir, target_stat


def _verified_project_target(
    path: Path,
) -> tuple[Path, Path | None, os.stat_result]:
    """Resolve an exact directory and classify an optional Git worktree root."""
    lexical = path.expanduser().absolute()
    try:
        resolved = lexical.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise SetforgeError(
            f"project target cannot be resolved: {lexical}: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise SetforgeError(f"project target is not a directory: {resolved}")
    try:
        result = run_git(resolved, ["rev-parse", "--show-toplevel"], check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        # A missing git binary or a hung git must not escape as a raw traceback.
        raise SetforgeError(
            f"project target Git probe failed for {resolved}: {exc}"
        ) from exc
    if result.returncode != 0:
        if (resolved / ".git").exists():
            detail = result.stderr.strip() or "unknown Git error"
            raise SetforgeError(f"project target has invalid Git metadata: {detail}")
        return resolved, None, resolved.stat()
    return _verified_git_worktree(resolved)


def _is_tracked(target: Path, relative: Path) -> bool:
    try:
        result = run_git(
            target,
            ["ls-files", "--error-unmatch", "--", relative.as_posix()],
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetforgeError(
            f"cannot classify Git destination {relative}: {exc}"
        ) from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or "unknown Git error"
    raise SetforgeError(f"cannot classify Git destination {relative}: {detail}")


def _created_parents(target: Path, destination: Path) -> tuple[Path, ...]:
    missing: list[Path] = []
    parent = destination.parent
    while parent != target:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            missing.append(parent)
            parent = parent.parent
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SetforgeError(f"project destination has an unsafe ancestor: {parent}")
        parent = parent.parent
    if parent == target:
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SetforgeError(f"project target changed during planning: {target}")
    return tuple(reversed(missing))


def _read_source(source: Path) -> tuple[bytes, int, str]:
    try:
        before = source.stat()
        if not stat.S_ISREG(before.st_mode):
            raise SetforgeError(f"project source is not a regular file: {source}")
        payload = source.read_bytes()
        after = source.stat()
    except OSError as exc:
        raise SetforgeError(f"project source cannot be read: {source}: {exc}") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise SetforgeError(f"project source changed while being read: {source}")
    return payload, stat.S_IMODE(before.st_mode), _sha256(payload)


def _plan_file(
    target: Path, resolved_file: ResolvedProjectFile, *, git: bool
) -> ProjectFilePlan:
    destination = target / resolved_file.dst
    try:
        destination.relative_to(target)
    except ValueError as exc:
        raise SetforgeError(
            f"project destination escapes target: {destination}"
        ) from exc
    parents = _created_parents(target, destination)
    payload, source_mode, source_digest = _read_source(resolved_file.src)
    applied_payload: bytes | None
    try:
        info = destination.lstat()
    except FileNotFoundError:
        action = ProjectFileAction.CREATE
        applied_payload = payload
        previous_payload = None
        previous_mode = None
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SetforgeError(
                f"project destination is not an ordinary regular file: {destination}"
            )
        tracked = git and _is_tracked(target, resolved_file.dst)
        if tracked:
            action = ProjectFileAction.OVERLAY
            applied_payload = None
        else:
            applied_payload = payload
        previous_payload = destination.read_bytes()
        previous_mode = stat.S_IMODE(info.st_mode)
        if not tracked and previous_payload == payload and previous_mode == source_mode:
            action = ProjectFileAction.RETAIN
        elif not tracked:
            action = ProjectFileAction.REPLACE
    return ProjectFilePlan(
        file_id=resolved_file.id,
        declaring_profile=resolved_file.declaring_profile,
        source=resolved_file.src,
        destination=destination,
        relative_destination=resolved_file.dst,
        source_payload=payload,
        source_mode=source_mode,
        source_digest=source_digest,
        applied_payload=applied_payload,
        action=action,
        previous_payload=previous_payload,
        previous_mode=previous_mode,
        created_parents=parents,
    )


def _existing_owner_id(config_root: Path) -> uuid.UUID | None:
    """Return this checkout's owner identity without creating one."""
    try:
        return read_owner_id(config_root)
    except SetforgeError:
        return None


def plan_injection(
    *,
    profile: str,
    target: Path,
    config_root: Path,
    config_path: Path | None = None,
    resolved: ResolvedProjectProfile,
    visibility: ProjectVisibility,
    read_owner: Callable[[Path], uuid.UUID | None] = _existing_owner_id,
) -> ProjectInjectionPlan:
    """Build a complete injection plan without changing target or state.

    ``read_owner`` lets a caller that already holds the config identity lock
    supply the owner, which the default would try to lock a second time.
    """
    root, git_dir, target_stat = _verified_project_target(target)
    state_path = manifest_path(root, profile)
    if state_path.exists():
        _refuse_existing_injection(
            profile, root, target_stat.st_ino, git_dir, state_path
        )
    files = tuple(
        _plan_file(root, item, git=git_dir is not None) for item in resolved.files
    )
    if git_dir is None:
        visibility_plan = None
        overlay_git_plan = None
    else:
        visibility_plan, overlay_git_plan = _plan_injection_visibility(
            target=root,
            manifest=state_path,
            git_dir=git_dir,
            profile=profile,
            files=files,
            visibilities=(visibility,) * len(files),
        )
    canonical_config_root = config_root.resolve(strict=True)
    canonical_config_path = (
        config_path.resolve(strict=True)
        if config_path is not None
        else (canonical_config_root / "setforge.yaml").resolve(strict=True)
    )
    try:
        canonical_config_path.relative_to(canonical_config_root)
    except ValueError as exc:
        raise SetforgeError(
            f"project config must be inside its config root: {canonical_config_path}"
        ) from exc
    plan = ProjectInjectionPlan(
        profile=profile,
        target=root,
        target_device=target_stat.st_dev,
        target_inode=target_stat.st_ino,
        git_dir=git_dir,
        visibility=visibility,
        config_root=canonical_config_root,
        config_path=canonical_config_path,
        files=files,
        manifest_path=state_path,
        visibility_plan=visibility_plan,
        overlay_git_plan=overlay_git_plan,
    )
    _refuse_claimed_destinations(plan, read_owner)
    return plan


def _sibling_destinations(plan: ProjectInjectionPlan) -> dict[str, tuple[str, str]]:
    """Map destinations recorded by other profiles at this target to their owner."""
    owners: dict[str, tuple[str, str]] = {}
    records = state_root() / "project-injections"
    for path in sorted(records.glob("*.json")) if records.is_dir() else ():
        if path == plan.manifest_path:
            continue
        try:
            raw = _load_manifest(path)
        except SetforgeError:
            continue
        raw_files = raw["files"]
        assert isinstance(raw_files, list)
        if raw["target"] != str(plan.target):
            continue
        for entry in raw_files:
            if isinstance(entry, dict):
                owners.setdefault(
                    str(entry.get("destination")),
                    (str(raw["profile"]), str(raw["target"])),
                )
    return owners


def refuse_unavailable_claim(
    claim: OwnershipClaim | None,
    relative: str,
    read_owner: Callable[[], uuid.UUID | None],
) -> None:
    """Refuse an active claim, or a released one that another checkout owns.

    A released claim keeps only the last injected content in its fingerprint,
    so the checkout that owns it may claim the destination again with new
    content, another profile, or after the directory moved.
    """
    if claim is None:
        return
    reference = claim.declaration_refs[0] if claim.declaration_refs else ""
    profile = reference.split(":")[1] if reference.count(":") >= 2 else "unknown"
    target = claim.locator.removesuffix(f"/{relative}")
    if claim.lifecycle is ClaimLifecycle.CLAIMED:
        raise SetforgeError(
            "a project destination already has an active ownership claim: "
            f"{relative} is injected by project profile {profile!r} at {target}; "
            f"run `setforge project remove {profile} {target}` first"
        )
    if claim.owner_id != read_owner():
        raise SetforgeError(
            "a project destination has a released ownership claim from another "
            f"config checkout: {relative} was injected by project profile "
            f"{profile!r} at {target} (owner {claim.owner_id}); inject it from "
            "that checkout, or inspect the claim with `setforge ownership list`"
        )


def _refuse_claimed_destinations(
    plan: ProjectInjectionPlan, read_owner: Callable[[Path], uuid.UUID | None]
) -> None:
    """Refuse, already in the preview, a destination that something else manages.

    Records are consulted besides the ledger because a claim written under an
    earlier device number is not found under the live one.
    """
    store = OwnershipStore()
    siblings = _sibling_destinations(plan)
    for item in plan.files:
        relative = item.relative_destination.as_posix()
        tracked = store.read(file_resource_id(item.destination))
        if (
            tracked is not None
            and tracked.authority is Authority.MANAGE
            and tracked.lifecycle is ClaimLifecycle.CLAIMED
        ):
            raise SetforgeError(
                "a project destination already has an active tracked-file "
                f"ownership claim: {item.destination} is managed by "
                f"{', '.join(tracked.declaration_refs)}"
            )
        claim = store.read(
            _resource_id(
                plan.target_device, plan.target_inode, item.relative_destination
            )
        )
        refuse_unavailable_claim(claim, relative, lambda: read_owner(plan.config_root))
        owner = siblings.get(relative)
        if owner is not None:
            raise SetforgeError(
                "a project destination already has an active ownership claim: "
                f"{relative} is injected by project profile {owner[0]!r} at "
                f"{owner[1]}; run `setforge project remove {owner[0]} {owner[1]}` "
                "first"
            )


def resolve_injection_plan(
    plan: ProjectInjectionPlan,
    *,
    auto: AutoResolution | None,
    interactive: bool,
) -> ProjectInjectionPlan | None:
    """Resolve initial tracked-file collisions without mutating state."""
    from setforge.project_sync import resolve_automatically, two_way_merge
    from setforge.reconcile.merge_model import Conflict
    from setforge.reconcile.types import FileId
    from setforge.ui.primitives import CANCEL

    resolved_files: list[ProjectFilePlan] = []
    for item in plan.files:
        if item.action is not ProjectFileAction.OVERLAY:
            resolved_files.append(item)
            continue
        assert item.previous_payload is not None
        result = two_way_merge(item.previous_payload, item.source_payload)
        if result.clean:
            merged = result.merged()
        elif auto is not None:
            merged = resolve_automatically(result, auto).merged()
        elif interactive:
            from setforge.reconcile.claude_merge import make_claude_merge_fn
            from setforge.reconcile.wizard import resolve_conflicts

            wizard = resolve_conflicts(
                FileId(f"project/{plan.profile}/{item.file_id}"),
                result,
                display_path=item.relative_destination.as_posix(),
                claude_merge=make_claude_merge_fn(
                    display_path=item.relative_destination.as_posix()
                ),
            )
            if wizard is CANCEL or wizard.deferred:
                return None
            merged = wizard.merged.merged()
        else:
            conflicts = sum(
                isinstance(segment, Conflict) for segment in result.segments
            )
            raise SetforgeError(
                f"project injection has {conflicts} unresolved tracked-file "
                f"conflict(s) in {item.relative_destination}; use a TTY or --auto"
            )
        assert isinstance(merged, bytes)
        overlay = build_overlay(
            plan.target,
            item.relative_destination,
            item.previous_payload,
            merged,
        )
        resolved_files.append(replace(item, applied_payload=merged, overlay=overlay))
    return replace(plan, files=tuple(resolved_files))


def _plan_injection_visibility(
    *,
    target: Path,
    manifest: Path,
    git_dir: Path,
    profile: str,
    files: tuple[ProjectFilePlan, ...],
    visibilities: tuple[ProjectVisibility, ...],
) -> tuple[VisibilityPlan, OverlayGitPlan | None]:
    for requested in ProjectVisibility:
        relative_paths = {
            item.relative_destination.as_posix()
            for item, item_visibility in zip(files, visibilities, strict=True)
            if item_visibility is requested
        }
        if relative_paths:
            _require_compatible_visibility(
                target=target,
                manifest=manifest,
                visibility=requested,
                relative_paths=relative_paths,
            )
    visibility_claims = tuple(
        VisibilityClaim(
            claim_id=claim_id(
                target_git_dir=git_dir,
                profile=profile,
                relative_path=item.relative_destination.as_posix(),
            ),
            relative_path=item.relative_destination.as_posix(),
        )
        for item, item_visibility in zip(files, visibilities, strict=True)
        if item.action is not ProjectFileAction.OVERLAY
        and item_visibility is ProjectVisibility.HIDDEN
    )
    overlay_claims = tuple(
        OverlayClaim(
            overlay_claim_id(
                git_dir=git_dir,
                profile=profile,
                relative_path=item.relative_destination.as_posix(),
            ),
            item.relative_destination.as_posix(),
        )
        for item, item_visibility in zip(files, visibilities, strict=True)
        if item.action is ProjectFileAction.OVERLAY
        and item_visibility is ProjectVisibility.HIDDEN
    )
    return (
        plan_claims(target, add=visibility_claims),
        plan_overlay_git(target, add=overlay_claims) if overlay_claims else None,
    )


def _sibling_exclude_path(raw: dict[str, object]) -> Path | None:
    """Return a recorded sibling's exclude file, or ``None`` for a stale one.

    A vanished directory, or one whose Git identity can no longer be read,
    keeps no intent to protect; hidden claims it left in a surviving
    repository are read from the exclude file itself.
    """
    other_target = Path(str(raw["target"]))
    if not other_target.is_dir():
        return None
    try:
        if raw["git_dir"] is None and _verified_project_target(other_target)[1] is None:
            return None
        return info_exclude_path(other_target)
    except SetforgeError:
        return None


def _require_compatible_visibility(
    *,
    target: Path,
    manifest: Path,
    visibility: ProjectVisibility,
    relative_paths: set[str],
    ignored_claim_ids: set[str] | None = None,
) -> None:
    """Refuse repository-common hidden/tracked ambiguity before mutation."""
    exclude_path, _, _, hidden_claims = read_claims(target)
    ignored = ignored_claim_ids or set()
    hidden_paths = {
        claim.relative_path for claim in hidden_claims if claim.claim_id not in ignored
    }
    if visibility is ProjectVisibility.TRACKED and relative_paths & hidden_paths:
        conflict = sorted(relative_paths & hidden_paths)[0]
        raise SetforgeError(
            "project visibility conflicts with another injection in this "
            f"repository for {conflict}: it is already hidden"
        )
    records = state_root() / "project-injections"
    if not records.is_dir():
        return
    for path in records.glob("*.json"):
        if path == manifest:
            continue
        try:
            raw, _payload = _read_record_document(path)
            if _sibling_exclude_path(raw) != exclude_path:
                continue
            _require_current_format(raw, path)
            raw_files = raw["files"]
            assert isinstance(raw_files, list)
            other_visibilities = {
                str(item["destination"]): ProjectVisibility(str(item.get("visibility")))
                for item in raw_files
                if isinstance(item, dict) and "destination" in item
            }
        except (AssertionError, OSError, SetforgeError, ValueError) as exc:
            raise SetforgeError(
                f"cannot validate sibling project visibility record: {path}: {exc}"
            ) from exc
        conflicts = sorted(relative_paths & other_visibilities.keys())
        mismatched = next(
            (
                relative
                for relative in conflicts
                if other_visibilities[relative] is not visibility
            ),
            None,
        )
        if mismatched is not None:
            other_visibility = other_visibilities[mismatched]
            raise SetforgeError(
                "project visibility conflicts with another injection in this "
                f"repository for {mismatched}: "
                f"recorded {other_visibility.value}, requested {visibility.value}"
            )


def _resource_id(device: int, inode: int, relative: Path) -> ResourceId:
    """Address one claim by the target identity recorded at injection.

    A network filesystem reports another device number after a remount, so the
    recorded number stays the ledger key while path, inode, and Git directory
    decide whether the worktree is still the same.
    """
    return ResourceId(
        kind="file",
        provider="project-profile",
        coordinate=relative.as_posix(),
        scope=ResourceScope._from_wire(
            ScopeKind.TARGET_ROOT, f"object:{device}:{inode}"
        ),
    )


def _claim_fingerprint(file: ProjectFilePlan) -> str:
    payload = json.dumps(
        {
            "digest": file.source_digest,
            "mode": file.source_mode,
            "path": file.relative_destination.as_posix(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return _sha256(payload)


def _claim_matches_plan(
    claim: OwnershipClaim,
    *,
    resource: ResourceId,
    owner_id: uuid.UUID,
    profile: str,
    item: ProjectFilePlan,
    lifecycle: ClaimLifecycle,
) -> bool:
    expected_facts = {
        ProvenanceFact(ProvenanceFactKind.ORIGIN, "project-profile"),
        ProvenanceFact(ProvenanceFactKind.ARTIFACT, item.source_digest),
    }
    return (
        claim.resource_id == resource
        and claim.owner_id == owner_id
        and claim.lifecycle is lifecycle
        and claim.declaration_refs == (f"project-profile:{profile}:{item.file_id}",)
        and expected_facts.issubset(claim.provenance)
        and claim.locator == str(item.destination)
        and claim.fingerprint == _claim_fingerprint(item)
    )


def _manifest_payload(plan: ProjectInjectionPlan, owner_id: uuid.UUID) -> bytes:
    files = []
    for item in plan.files:
        applied_payload = item.applied_payload
        if applied_payload is None:
            raise SetforgeError(
                f"project file resolution is incomplete: {item.relative_destination}"
            )
        files.append(
            {
                "action": item.action.value,
                "applied_digest": _sha256(applied_payload),
                "applied_mode": (
                    item.previous_mode
                    if item.action is ProjectFileAction.OVERLAY
                    else item.source_mode
                ),
                "applied_payload": base64.b64encode(applied_payload).decode("ascii"),
                "created_parents": [
                    parent.relative_to(plan.target).as_posix()
                    for parent in item.created_parents
                ],
                "declaring_profile": item.declaring_profile,
                "file_id": item.file_id,
                "previous_mode": item.previous_mode,
                "previous_payload": (
                    base64.b64encode(item.previous_payload).decode("ascii")
                    if item.previous_payload is not None
                    else None
                ),
                "source": str(item.source),
                "source_digest": item.source_digest,
                "upstream_mode": item.source_mode,
                "upstream_payload": base64.b64encode(item.source_payload).decode(
                    "ascii"
                ),
                "destination": item.relative_destination.as_posix(),
                "visibility": plan.visibility.value,
            }
        )
    payload = {
        "config_owner_id": str(owner_id),
        "config_path": str(plan.config_path),
        "config_root": str(plan.config_root),
        "files": files,
        "git_dir": str(plan.git_dir) if plan.git_dir is not None else None,
        "profile": plan.profile,
        "schema": _MANIFEST_SCHEMA,
        "target": str(plan.target),
        "target_device": plan.target_device,
        "target_inode": plan.target_inode,
        "visibility": plan.visibility.value,
    }
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _read_record_document(path: Path) -> tuple[dict[str, object], bytes]:
    """Read a record of any known format and check its top-level fields.

    Conversion is the only reader of an older record's content. Other callers
    use this for a record's address alone: to drop a stale record, or to skip
    one that belongs to another directory before requiring the current format.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        raise SetforgeError(f"project injection is not recorded: {path}") from exc
    except OSError as exc:
        raise SetforgeError(
            f"project injection state is corrupt: {path}: {exc}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
            raise SetforgeError(
                f"project injection state is not a bounded file: {path}"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(16 * 1024 * 1024 + 1)
        if len(payload) > 16 * 1024 * 1024:
            raise SetforgeError(f"project injection state is too large: {path}")
        raw = json.loads(payload)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise SetforgeError(
            f"project injection state is corrupt: {path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    schema = raw.get("schema") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema
        not in {
            _LEGACY_MANIFEST_SCHEMA,
            _PRIOR_MANIFEST_SCHEMA,
            _MANIFEST_SCHEMA,
        }
    ):
        raise SetforgeError(
            f"project injection state has an unsupported schema: {path}"
        )
    required = {
        "config_owner_id",
        "config_root",
        "files",
        "git_dir",
        "profile",
        "schema",
        "target",
        "target_device",
        "target_inode",
        "visibility",
    }
    if schema != _LEGACY_MANIFEST_SCHEMA:
        required.add("config_path")
    if (
        set(raw) != required
        or not isinstance(raw["files"], list)
        or any(
            not isinstance(raw[field], int) or isinstance(raw[field], bool)
            for field in ("target_device", "target_inode")
        )
    ):
        raise SetforgeError(f"project injection state has invalid fields: {path}")
    return raw, payload


def _require_current_format(raw: dict[str, object], path: Path) -> None:
    """Refuse an older record, naming the command that settles it.

    Conversion skips a record whose directory is gone or was replaced, so
    that record can only be dropped.
    """
    if raw["schema"] == _MANIFEST_SCHEMA:
        return
    target = Path(str(raw["target"]))
    try:
        stale = (
            not target.is_dir()
            or target.resolve() != target
            or target.stat().st_ino != raw["target_inode"]
        )
    except OSError:
        stale = True
    remedy = (
        f"`setforge project remove {raw['profile']} {target}` to drop it"
        if stale
        else f"`setforge project sync {target}` to convert it"
    )
    raise SetforgeError(
        f"project injection record is in an older format: {path}; run {remedy}"
    )


def _load_manifest_payload(path: Path) -> tuple[dict[str, object], bytes]:
    """Load and validate a manifest while retaining its exact bound bytes."""
    raw, payload = _read_record_document(path)
    _require_current_format(raw, path)
    return raw, payload


def _load_manifest(path: Path) -> dict[str, object]:
    raw, _payload = _load_manifest_payload(path)
    return raw


def _refuse_existing_injection(
    profile: str, target: Path, inode: int, git_dir: Path | None, record: Path
) -> None:
    """Refuse a second injection, naming the command that applies instead."""
    raw = _load_manifest(record)
    if raw["target_inode"] != inode:
        raise SetforgeError(
            f"a stale injection record exists for {target}; run "
            f"`setforge project remove {profile} {target}` to drop it, "
            "then inject again"
        )
    remedy = identity_remedy(raw, target, inode, git_dir)
    if remedy is not None:
        raise SetforgeError(
            f"project profile {profile!r} is already injected at {target}, but {remedy}"
        )
    raise SetforgeError(
        f"project profile {profile!r} is already injected at {target}; use "
        f"`setforge project sync {target}`"
    )


def _require_guards(guards: MutationLockGuards, target: Path) -> None:
    if len(guards.targets) != 1 or guards.targets[0].target != target:
        raise SetforgeError("project target lock binding is invalid")
    guards.verify_targets()


def _config_identity_fd(guards: MutationLockGuards) -> int:
    identity = guards.config_identity
    if identity is None:
        raise SetforgeError("project config identity lock is missing")
    return identity.directory_fd


def _require_config_owner(
    guards: MutationLockGuards, config_root: Path, expected: uuid.UUID
) -> None:
    if read_owner_id_locked(config_root, _config_identity_fd(guards)) != expected:
        raise SetforgeError("project injection belongs to a different config checkout")


def _exclude_paths(plan: VisibilityPlan | None) -> tuple[Path, ...]:
    return (plan.exclude_path,) if plan is not None else ()


def _overlay_git_paths(plan: OverlayGitPlan | None) -> tuple[Path, ...]:
    return (plan.config_path, plan.attributes_path) if plan is not None else ()


@contextmanager
def _project_transaction(
    *,
    command: str,
    profile: str,
    config_dir: Path | None,
    config_dirs: tuple[Path, ...] = (),
    paths: tuple[Path, ...],
    checkpoint: str,
    recovery: str,
) -> Iterator[None]:
    """Journal the caller's mutations of ``paths`` as one reversible checkpoint.

    Runs under the caller's mutation locks. A failure inside the block rolls
    every path back before the exception continues.
    """
    journal = operations.prepare(
        command=command,
        profile=profile,
        config_dir=config_dir,
        config_dirs=config_dirs,
        resources_lock=True,
        paths=paths,
        path_guards=capture_parent_path_guards(paths),
    )
    with operations.recover_on_error(profile, command):
        journal = operations.begin_checkpoint(
            journal,
            name=checkpoint,
            kind=operations.CheckpointKind.REVERSIBLE,
            recovery=recovery,
            paths=paths,
            restore_state=False,
            restore_transitions=False,
        )
        yield
        journal = operations.finish_checkpoint(journal)
        operations.complete(journal)


@contextmanager
def _relative_parent(
    guard: TargetLockGuard, relative: Path, *, create: bool
) -> Iterator[int]:
    """Open a target-relative parent without following any symlink component."""
    guard.verify_expected()
    if guard.target_fd is None or relative.is_absolute() or ".." in relative.parts:
        raise SetforgeError("project destination lost its target-root binding")
    descriptor = os.dup(guard.target_fd)
    try:
        for component in relative.parent.parts:
            if component in {"", "."}:
                continue
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise SetforgeError(
                        f"project destination parent disappeared: {relative.parent}"
                    ) from None
                os.mkdir(component, mode=0o755, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise SetforgeError(
                    f"project destination parent is unsafe: {relative.parent}: {exc}"
                ) from exc
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _write_project_file(
    guard: TargetLockGuard, relative: Path, payload: bytes, mode: int
) -> None:
    """Publish one project file through a descriptor-confined atomic write."""
    try:
        with _relative_parent(guard, relative, create=True) as parent_fd:
            atomicio.atomic_write_bytes_at(parent_fd, relative.name, payload, mode=mode)
    except OSError as exc:
        raise SetforgeError(
            f"project file cannot be written: {guard.target / relative}: {exc}"
        ) from exc


def _unlink_project_file(guard: TargetLockGuard, relative: Path) -> None:
    try:
        with _relative_parent(guard, relative, create=False) as parent_fd:
            os.unlink(relative.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except OSError as exc:
        raise SetforgeError(
            f"project file cannot be removed: {guard.target / relative}: {exc}"
        ) from exc


def _require_writable_parents(paths: Iterable[Path]) -> None:
    """Refuse before journaling: rollback cannot restore a read-only directory."""
    for path in paths:
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK | os.X_OK):
            raise SetforgeError(f"project directory is not writable: {parent}")


def _remove_created_parent(guard: TargetLockGuard, relative: Path) -> None:
    guard.verify_expected()
    try:
        (guard.target / relative).lstat()
    except FileNotFoundError:
        # A user deletion or another member's cleanup may have removed it.
        return
    with _relative_parent(guard, relative, create=False) as parent_fd:
        try:
            os.rmdir(relative.name, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno not in {39, 66}:
                raise SetforgeError(
                    "project directory cannot be removed: "
                    f"{guard.target / relative}: {exc}"
                ) from exc
        else:
            os.fsync(parent_fd)


def _injected_content(path: Path, destination: Path, entry: dict[str, object]) -> bytes:
    """Recover the bytes a format-1 record names only by digest."""
    for candidate in (destination, Path(str(entry.get("source")))):
        try:
            if not stat.S_ISREG(candidate.lstat().st_mode):
                continue
            payload = candidate.read_bytes()
        except OSError:
            continue
        if _sha256(payload) == entry.get("applied_digest"):
            return payload
    raise SetforgeError(
        f"project injection record is in an older format and cannot be converted: "
        f"{path}: {destination} and its profile source both differ from the "
        "injected content, which this record does not hold. Run `setforge project "
        "sync` with SetForge 1.3 or 1.4, or put the injected content back"
    )


def _converted_record(
    path: Path, raw: dict[str, object], root: Path, git_dir: Path | None
) -> bytes:
    """Render an older record in the current format, adding only what it lacks.

    The result must pass the current format's own validation, so a malformed
    older record is refused instead of converted.
    """
    invalid = SetforgeError(f"project injection state has invalid fields: {path}")
    legacy = raw["schema"] == _LEGACY_MANIFEST_SCHEMA
    document = dict(raw)
    if legacy:
        try:
            document["config_path"] = str(
                (Path(str(raw["config_root"])) / "setforge.yaml").resolve(strict=True)
            )
        except OSError as exc:
            raise SetforgeError(
                f"project injection config cannot be resolved safely: {path}"
            ) from exc
    recorded_git_dir = raw["git_dir"]
    entries: list[dict[str, object]] = []
    raw_files = raw["files"]
    assert isinstance(raw_files, list)
    for value in raw_files:
        if not isinstance(value, dict) or "visibility" in value:
            raise invalid
        entry = dict(value)
        relative = Path(str(entry.get("destination", "")))
        if relative.is_absolute() or relative == Path() or ".." in relative.parts:
            raise invalid
        overlay = entry.get("action") == ProjectFileAction.OVERLAY.value
        if legacy:
            added = {"applied_payload", "upstream_mode", "upstream_payload"}
            if overlay or added & entry.keys():
                raise invalid
            content = base64.b64encode(
                _injected_content(path, root / relative, entry)
            ).decode("ascii")
            entry["applied_payload"] = entry["upstream_payload"] = content
            entry["upstream_mode"] = entry.get("applied_mode")
        entry["visibility"] = raw["visibility"]
        if overlay and git_dir is not None and isinstance(recorded_git_dir, str):
            # These formats filtered every tracked file whatever visibility was
            # recorded, so record what the repository actually holds.
            claim = OverlayClaim(
                overlay_claim_id(
                    git_dir=Path(recorded_git_dir),
                    profile=str(raw["profile"]),
                    relative_path=relative.as_posix(),
                ),
                relative.as_posix(),
            )
            entry["visibility"] = (
                ProjectVisibility.TRACKED
                if plan_overlay_git(root, add=(claim,)).added
                else ProjectVisibility.HIDDEN
            ).value
        entries.append(entry)
    document["schema"] = _MANIFEST_SCHEMA
    document["files"] = entries
    _record_files(document, target=root)
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode()


def convert_older_records(target: Path) -> tuple[str, ...]:
    """Rewrite every older-format record of ``target`` in the current format.

    Returns the converted profiles. Each record is rewritten under the locks
    and journal of any other record mutation; project files and Git state are
    never touched. A record whose directory is gone or was replaced is left
    for ``project remove`` to drop.
    """
    lexical = Path(os.path.normpath(target.expanduser().absolute()))
    records = state_root() / "project-injections"
    if not lexical.is_dir() or not records.is_dir():
        return ()
    root, _git_dir, _info = _verified_project_target(lexical)
    converted: list[str] = []
    for path in sorted(records.glob("*.json")):
        try:
            with path.open("rb") as handle:
                listed = json.load(handle)
        except (OSError, ValueError):
            continue
        if (
            not isinstance(listed, dict)
            or listed.get("schema")
            not in (_LEGACY_MANIFEST_SCHEMA, _PRIOR_MANIFEST_SCHEMA)
            or listed.get("target") != str(root)
        ):
            continue
        raw, _payload = _read_record_document(path)
        try:
            config_root = Path(str(raw["config_root"])).resolve(strict=True)
        except OSError as exc:
            raise SetforgeError(
                f"project injection config cannot be resolved safely: {path}"
            ) from exc
        operation_profile = f"project-{_injection_key(root, str(raw['profile']))}"
        with mutation_locks(
            resources=True,
            config_dir=config_root,
            target_roots=(root,),
            profile=operation_profile,
        ) as guards:
            _require_guards(guards, root)
            _root, git_dir, info = _verified_project_target(root)
            raw, before = _read_record_document(path)
            if raw["schema"] == _MANIFEST_SCHEMA or raw["target_inode"] != info.st_ino:
                continue
            after = _converted_record(path, raw, root, git_dir)
            with _project_transaction(
                command="project-convert",
                profile=operation_profile,
                config_dir=config_root,
                paths=(path,),
                checkpoint="convert-project-record",
                recovery="restore the project injection record",
            ):
                if path.read_bytes() != before:
                    raise SetforgeError("project injection record changed; retry")
                atomicio.atomic_write_bytes(path, after, mode=0o600)
        converted.append(str(raw["profile"]))
    return tuple(converted)


def apply_injection(  # noqa: C901 - one fail-closed journaled transaction
    plan: ProjectInjectionPlan,
) -> None:
    """Apply a previously confirmed plan as one journaled transaction."""
    operation_profile = f"project-{_injection_key(plan.target, plan.profile)}"
    with mutation_locks(
        resources=True,
        config_identity_dir=resolve_owner_common_dir(plan.config_root),
        config_dir=plan.config_root,
        target_roots=(plan.target,),
        profile=operation_profile,
    ) as guards:
        _require_guards(guards, plan.target)
        identity_fd = _config_identity_fd(guards)

        def locked_owner(config_root: Path) -> uuid.UUID | None:
            try:
                return read_owner_id_locked(config_root, identity_fd)
            except SetforgeError:
                return None

        fresh = plan_injection(
            profile=plan.profile,
            target=plan.target,
            config_root=plan.config_root,
            config_path=plan.config_path,
            resolved=_resolved_from_plan(plan),
            visibility=plan.visibility,
            read_owner=locked_owner,
        )
        unresolved_files = tuple(
            replace(item, applied_payload=None, overlay=None)
            if item.action is ProjectFileAction.OVERLAY
            else item
            for item in plan.files
        )
        if fresh != replace(plan, files=unresolved_files):
            raise SetforgeError("project injection plan changed before apply; retry")
        store = OwnershipStore()
        resources = tuple(
            _resource_id(
                fresh.target_device, fresh.target_inode, item.relative_destination
            )
            for item in plan.files
        )
        claims = tuple(store.read(resource) for resource in resources)
        tracked_claims = tuple(
            store.read(file_resource_id(item.destination)) for item in plan.files
        )
        if any(
            claim is not None
            and claim.authority is Authority.MANAGE
            and claim.lifecycle is ClaimLifecycle.CLAIMED
            for claim in tracked_claims
        ):
            raise SetforgeError(
                "a project destination already has an active tracked-file "
                "ownership claim"
            )
        owner_id = load_or_create_owner_id_locked(
            plan.config_root, identity_fd, uuid.uuid4()
        )
        for item, claim in zip(plan.files, claims, strict=True):
            refuse_unavailable_claim(
                claim, item.relative_destination.as_posix(), lambda: owner_id
            )
        paths = (
            *(item.destination for item in plan.files),
            *(
                overlay_path(plan.target, item.relative_destination)
                for item in plan.files
                if item.action is ProjectFileAction.OVERLAY
            ),
            *_overlay_git_paths(plan.overlay_git_plan),
            plan.manifest_path,
            *(store.claim_path(resource) for resource in resources),
            *_exclude_paths(plan.visibility_plan),
        )
        _require_writable_parents(
            item.destination
            for item in plan.files
            if item.action is not ProjectFileAction.RETAIN
        )
        with _project_transaction(
            command="project-inject",
            profile=operation_profile,
            config_dir=plan.config_root,
            paths=paths,
            checkpoint="materialize-project-files-and-state",
            recovery="restore project files, manifest, and ownership claims",
        ):
            for item, resource, prior_claim in zip(
                plan.files, resources, claims, strict=True
            ):
                guards.verify_targets()
                if item.action is not ProjectFileAction.RETAIN:
                    if item.applied_payload is None:
                        raise SetforgeError(
                            f"project file resolution is incomplete: "
                            f"{item.relative_destination}"
                        )
                    _write_project_file(
                        guards.targets[0],
                        item.relative_destination,
                        item.applied_payload,
                        item.previous_mode
                        if item.action is ProjectFileAction.OVERLAY
                        and item.previous_mode is not None
                        else item.source_mode,
                    )
                if item.overlay is not None:
                    write_overlay(item.overlay)
                expected_generation = None
                if prior_claim is not None:
                    restored = store.restore_locked(prior_claim)
                    expected_generation = restored.generation
                store.claim_locked(
                    resource_id=resource,
                    owner_id=owner_id,
                    declaration_refs=(
                        f"project-profile:{plan.profile}:{item.file_id}",
                    ),
                    provenance=(
                        ProvenanceFact(ProvenanceFactKind.ORIGIN, "project-profile"),
                        ProvenanceFact(ProvenanceFactKind.ARTIFACT, item.source_digest),
                    ),
                    locator=str(item.destination),
                    fingerprint=_claim_fingerprint(item),
                    expected_generation=expected_generation,
                )
            if plan.visibility_plan is not None:
                apply_claims(plan.visibility_plan)
            if plan.overlay_git_plan is not None:
                apply_overlay_git(plan.overlay_git_plan)
            atomicio.atomic_write_bytes(
                plan.manifest_path, _manifest_payload(plan, owner_id), mode=0o600
            )


def _resolved_from_plan(plan: ProjectInjectionPlan) -> ResolvedProjectProfile:
    return ResolvedProjectProfile(
        default_visibility=plan.visibility,
        files=tuple(
            ResolvedProjectFile(
                id=item.file_id,
                declaring_profile=item.declaring_profile,
                src=item.source,
                dst=item.relative_destination,
            )
            for item in plan.files
        ),
    )


def plan_removal(  # noqa: C901 - one fail-closed parser for untrusted state
    *,
    profile: str,
    target: Path,
    config_path: Path,
    require_profile_content: bool = True,
) -> ProjectRemovePlan:
    """Load and drift-check the exact injection to remove.

    A sync may record merged bytes that still hold local edits. Removal would
    discard them, so by default an ordinary file must equal the profile bytes.
    A missing file holds nothing to discard, so that default mode accepts it;
    a caller validating the record for another purpose still gets an error.
    Removal writes a missing file's saved pre-injection content back. Two
    missing files stay absent: one that did not exist before injection, and a
    tracked-overlay file that Git's index no longer has (after ``git rm``, or
    on a branch without it), which the plan lists in ``git_removed``.
    """
    root, git_dir, target_stat = _verified_project_target(target)
    canonical_config_path = config_path.resolve(strict=True)
    config_root = canonical_config_path.parent
    state_path = manifest_path(root, profile)
    raw = _load_manifest(state_path)
    if (
        raw["profile"] != profile
        or raw["target"] != str(root)
        or raw["target_inode"] != target_stat.st_ino
        or raw["config_root"] != str(config_root)
        or raw["config_path"] != str(canonical_config_path)
    ):
        raise SetforgeError(
            "project injection state belongs to a different config manifest"
        )
    try:
        owner_id = uuid.UUID(str(raw["config_owner_id"]))
    except (ValueError, TypeError) as exc:
        raise SetforgeError(
            "project injection state has an invalid owner identity"
        ) from exc
    files: list[ProjectFilePlan] = []
    file_visibilities: dict[Path, ProjectVisibility] = {}
    all_parents: set[Path] = set()
    git_removed: list[Path] = []
    for stored in _record_files(raw, target=root):
        relative = stored.destination
        destination = root / relative
        action = stored.action
        source_digest = stored.source_digest
        applied_payload = stored.applied_payload
        applied_digest = stored.applied_digest
        applied_mode = stored.applied_mode
        upstream_payload = stored.upstream_payload
        upstream_mode = stored.upstream_mode
        previous_payload = stored.previous_payload
        previous_mode = stored.previous_mode
        parents = stored.created_parents
        try:
            _created_parents(root, destination)
            info = destination.lstat()
        except ValueError as exc:
            raise SetforgeError(
                f"injected project file is unsafe: {destination}"
            ) from exc
        except FileNotFoundError:
            info = None
        live_payload = destination.read_bytes() if info is not None else None
        applied_absent = (
            applied_payload is None and applied_digest is None and applied_mode is None
        )
        overlay: ProjectOverlay | None = None
        live_matches_absent = info is None and applied_absent
        if not require_profile_content:
            expected_digest, expected_mode = applied_digest, applied_mode
        else:
            expected_digest, expected_mode = source_digest, upstream_mode
        live_matches_present = (
            info is not None
            and not stat.S_ISLNK(info.st_mode)
            and stat.S_ISREG(info.st_mode)
            and live_payload is not None
            and expected_digest is not None
            and _sha256(live_payload) == expected_digest
            and stat.S_IMODE(info.st_mode) == expected_mode
        )
        removable_missing = info is None and require_profile_content
        if action is ProjectFileAction.OVERLAY:
            if (
                applied_payload is None
                or previous_payload is None
                or applied_mode is None
                or (
                    not removable_missing
                    and (
                        info is None
                        or live_payload is None
                        or stat.S_ISLNK(info.st_mode)
                        or not stat.S_ISREG(info.st_mode)
                        or stat.S_IMODE(info.st_mode) != applied_mode
                    )
                )
            ):
                raise SetforgeError(
                    f"tracked project overlay has invalid state: {destination}"
                )
            overlay = read_overlay(root, relative)
            if (
                overlay is None
                or overlay.base != previous_payload
                or overlay.local != applied_payload
            ):
                raise SetforgeError(
                    f"tracked project overlay state is missing or mismatched: "
                    f"{destination}"
                )
            # Content Git checked out without the filter is already clean.
            if live_payload is not None and live_payload != overlay.base:
                clean_content(overlay, live_payload)
            live_matches_present = True
            if info is None and git_dir is not None and not _is_tracked(root, relative):
                git_removed.append(relative)
        if info is None and not live_matches_absent and not removable_missing:
            raise SetforgeError(
                f"injected project file is missing: {destination}; "
                f"{missing_file_remedy(root, profile, action)}"
            )
        # A local file that a sync kept instead of the profile content already
        # equals what removal restores, so removing it writes nothing.
        live_is_baseline = (
            action is not ProjectFileAction.OVERLAY
            and info is not None
            and stat.S_ISREG(info.st_mode)
            and live_payload == previous_payload
            and stat.S_IMODE(info.st_mode) == previous_mode
        )
        if (
            not live_matches_absent
            and not live_matches_present
            and not live_is_baseline
            and not removable_missing
        ):
            mode = f"{expected_mode:04o}" if expected_mode is not None else "none"
            raise SetforgeError(
                f"injected project file has drifted: {destination}; its content or "
                f"mode (expected {mode}) differs from what was injected and removal "
                "would discard the difference. Save your changes elsewhere, delete "
                "the file, then remove again"
            )
        all_parents.update(parents)
        file_visibilities[relative] = stored.visibility
        files.append(
            ProjectFilePlan(
                file_id=stored.file_id,
                declaring_profile=stored.declaring_profile,
                source=stored.source,
                destination=destination,
                relative_destination=relative,
                source_payload=upstream_payload,
                source_mode=upstream_mode,
                source_digest=source_digest,
                applied_payload=applied_payload,
                action=action,
                previous_payload=previous_payload,
                previous_mode=previous_mode,
                created_parents=parents,
                overlay=overlay,
            )
        )
    # The same directory may have gained, lost, or changed its Git directory
    # since injection. Claims are keyed by the recorded one, and only entries
    # the live repository still holds can be released.
    recorded_git_dir = raw["git_dir"]
    claim_git_dir = (
        Path(recorded_git_dir)
        if git_dir is not None and isinstance(recorded_git_dir, str)
        else None
    )
    hidden_to_remove: tuple[VisibilityClaim, ...] = ()
    if claim_git_dir is not None:
        expected_claims = tuple(
            VisibilityClaim(
                claim_id=claim_id(
                    target_git_dir=claim_git_dir,
                    profile=profile,
                    relative_path=item.relative_destination.as_posix(),
                ),
                relative_path=item.relative_destination.as_posix(),
            )
            for item in files
            if item.action is not ProjectFileAction.OVERLAY
            and file_visibilities[item.relative_destination] is ProjectVisibility.HIDDEN
        )
        _, _, _, current_claims = read_claims(root)
        current_by_id = {claim.claim_id: claim for claim in current_claims}
        for claim in expected_claims:
            observed = current_by_id.get(claim.claim_id)
            if observed is not None and observed != claim:
                raise SetforgeError("Git visibility claim identity collides")
        hidden_to_remove = tuple(
            claim for claim in expected_claims if claim.claim_id in current_by_id
        )
    visibility_plan = (
        plan_claims(root, remove=hidden_to_remove) if git_dir is not None else None
    )
    overlay_to_remove = tuple(
        OverlayClaim(
            overlay_claim_id(
                git_dir=claim_git_dir,
                profile=profile,
                relative_path=item.relative_destination.as_posix(),
            ),
            item.relative_destination.as_posix(),
        )
        for item in files
        if claim_git_dir is not None
        and item.action is ProjectFileAction.OVERLAY
        and file_visibilities[item.relative_destination] is ProjectVisibility.HIDDEN
    )
    if visibility_plan is not None and claim_git_dir != git_dir:
        held = set(
            read_overlay_claims(visibility_plan.exclude_path.with_name("attributes"))
        )
        overlay_to_remove = tuple(claim for claim in overlay_to_remove if claim in held)
    overlay_git_plan = (
        plan_overlay_git(root, remove=overlay_to_remove) if overlay_to_remove else None
    )
    recorded_device = raw["target_device"]
    assert isinstance(recorded_device, int)
    return ProjectRemovePlan(
        profile=profile,
        target=root,
        target_device=recorded_device,
        target_inode=target_stat.st_ino,
        config_path=canonical_config_path,
        manifest_path=state_path,
        owner_id=owner_id,
        files=tuple(files),
        created_parents=tuple(
            sorted(all_parents, key=lambda path: len(path.parts), reverse=True)
        ),
        visibility_plan=visibility_plan,
        overlay_git_plan=overlay_git_plan,
        git_removed=tuple(git_removed),
    )


def _overlay_removal_content(
    item: ProjectFilePlan, *, git_removed: bool
) -> bytes | None:
    """Return what removal writes to a tracked overlay file, ``None`` for nothing.

    A missing file gets its saved pre-injection content back, which may hold
    lines Git never had, unless Git itself removed the path from its index.
    """
    if item.overlay is None:
        raise SetforgeError("tracked project overlay state is missing")
    if item.destination.exists():
        live = item.destination.read_bytes()
        if live == item.overlay.base:
            return live
        return clean_content(item.overlay, live)
    return None if git_removed else item.overlay.base


def _restore_planned_files(
    plan: ProjectRemovePlan,
    resources: tuple[ResourceId, ...],
    claims: tuple[OwnershipClaim | None, ...],
    store: OwnershipStore,
    guards: MutationLockGuards,
) -> None:
    for item, resource, claim in zip(plan.files, resources, claims, strict=True):
        guards.verify_targets()
        if item.action is ProjectFileAction.CREATE:
            try:
                item.destination.lstat()
            except FileNotFoundError:
                pass
            else:
                _unlink_project_file(guards.targets[0], item.relative_destination)
        elif item.action is ProjectFileAction.OVERLAY:
            restored = _overlay_removal_content(
                item, git_removed=item.relative_destination in plan.git_removed
            )
            if restored is not None:
                _write_project_file(
                    guards.targets[0],
                    item.relative_destination,
                    restored,
                    (
                        item.previous_mode
                        if item.previous_mode is not None
                        else item.source_mode
                    ),
                )
        else:
            if item.previous_payload is None or item.previous_mode is None:
                raise SetforgeError("project injection restoration baseline is corrupt")
            current_payload: bytes | None = (
                item.destination.read_bytes() if item.destination.exists() else None
            )
            current_mode = (
                stat.S_IMODE(item.destination.stat().st_mode)
                if item.destination.exists()
                else None
            )
            if (
                current_payload != item.previous_payload
                or current_mode != item.previous_mode
            ):
                _write_project_file(
                    guards.targets[0],
                    item.relative_destination,
                    item.previous_payload,
                    item.previous_mode,
                )
        if claim is None:
            raise SetforgeError("project injection ownership state changed")
        store.release_locked(
            resource,
            expected_owner=plan.owner_id,
            expected_generation=claim.generation,
        )


def _removal_changes_file(item: ProjectFilePlan) -> bool:
    if item.action is ProjectFileAction.OVERLAY:
        return True
    try:
        info = item.destination.lstat()
    except FileNotFoundError:
        return item.action is not ProjectFileAction.CREATE
    return item.action is ProjectFileAction.CREATE or (
        item.destination.read_bytes() != item.previous_payload
        or stat.S_IMODE(info.st_mode) != item.previous_mode
    )


def apply_removal(plan: ProjectRemovePlan) -> None:
    """Restore one drift-free injection and retire its private state."""
    operation_profile = f"project-{_injection_key(plan.target, plan.profile)}"
    config_root = plan.config_path.parent
    with mutation_locks(
        resources=True,
        config_identity_dir=resolve_owner_common_dir(config_root),
        config_dir=config_root,
        target_roots=(plan.target,),
        profile=operation_profile,
    ) as guards:
        _require_guards(guards, plan.target)
        fresh = plan_removal(
            profile=plan.profile,
            target=plan.target,
            config_path=plan.config_path,
        )
        if fresh != plan:
            raise SetforgeError("project removal plan changed before apply; retry")
        refuse_active_file_claims(item.destination for item in plan.files)
        _require_config_owner(guards, config_root, plan.owner_id)
        store = OwnershipStore()
        resources = tuple(
            _resource_id(
                plan.target_device, plan.target_inode, item.relative_destination
            )
            for item in plan.files
        )
        claims = tuple(store.read(resource) for resource in resources)
        if any(
            claim is None
            or not _claim_matches_plan(
                claim,
                resource=resource,
                owner_id=plan.owner_id,
                profile=plan.profile,
                item=item,
                lifecycle=ClaimLifecycle.CLAIMED,
            )
            for item, resource, claim in zip(plan.files, resources, claims, strict=True)
        ):
            raise SetforgeError(
                "project injection ownership state is missing or mismatched"
            )
        overlay_paths = tuple(
            overlay_path(plan.target, item.relative_destination)
            for item in plan.files
            if item.action is ProjectFileAction.OVERLAY
        )
        paths = (
            *(item.destination for item in plan.files),
            *overlay_paths,
            *_overlay_git_paths(plan.overlay_git_plan),
            *plan.created_parents,
            plan.manifest_path,
            *(store.claim_path(resource) for resource in resources),
            *_exclude_paths(plan.visibility_plan),
        )
        _require_writable_parents(
            (
                *(
                    item.destination
                    for item in plan.files
                    if _removal_changes_file(item)
                ),
                *(parent for parent in plan.created_parents if parent.exists()),
            )
        )
        with _project_transaction(
            command="project-remove",
            profile=operation_profile,
            config_dir=config_root,
            paths=paths,
            checkpoint="restore-project-files-and-retire-state",
            recovery="restore injected files, manifest, and ownership claims",
        ):
            _restore_planned_files(plan, resources, claims, store, guards)
            if plan.visibility_plan is not None:
                apply_claims(plan.visibility_plan)
            if plan.overlay_git_plan is not None:
                apply_overlay_git(plan.overlay_git_plan)
            for overlay_state in overlay_paths:
                overlay_state.unlink()
            for parent in plan.created_parents:
                _remove_created_parent(
                    guards.targets[0], parent.relative_to(plan.target)
                )
            plan.manifest_path.unlink()


def _stale_record_claims(
    raw: dict[str, object], live: os.stat_result | None
) -> tuple[str, tuple[ResourceId, ...]] | None:
    """Return why a record's directory is gone for good, and its claim keys.

    Only a missing or replaced directory qualifies. The same directory with a
    changed Git directory still holds the injected files and is removed normally.
    """
    if live is None:
        reason = "project directory no longer exists"
    elif raw["target_inode"] != live.st_ino:
        reason = "project directory was replaced since injection"
    else:
        return None
    device, inode, raw_files = raw["target_device"], raw["target_inode"], raw["files"]
    assert isinstance(device, int)
    assert isinstance(inode, int)
    assert isinstance(raw_files, list)
    resources: list[ResourceId] = []
    for entry in raw_files:
        relative = Path(
            str(entry.get("destination", "")) if isinstance(entry, dict) else ""
        )
        if relative.is_absolute() or relative == Path() or ".." in relative.parts:
            raise SetforgeError("project injection state has an invalid file record")
        resources.append(_resource_id(device, inode, relative))
    return reason, tuple(resources)


def _stale_git_plans(
    root: Path, recorded_git_dir: Path, profile: str, relatives: tuple[str, ...]
) -> tuple[VisibilityPlan | None, OverlayGitPlan | None]:
    """Plan releasing only the private Git entries this injection still holds."""
    exclude_path, _, _, hidden_claims = read_claims(root)
    hidden = set(hidden_claims)
    filtered = set(read_overlay_claims(exclude_path.with_name("attributes")))
    hidden_to_remove = tuple(
        claim
        for claim in (
            VisibilityClaim(
                claim_id(
                    target_git_dir=recorded_git_dir,
                    profile=profile,
                    relative_path=relative,
                ),
                relative,
            )
            for relative in relatives
        )
        if claim in hidden
    )
    overlay_to_remove = tuple(
        claim
        for claim in (
            OverlayClaim(
                overlay_claim_id(
                    git_dir=recorded_git_dir, profile=profile, relative_path=relative
                ),
                relative,
            )
            for relative in relatives
        )
        if claim in filtered
    )
    return (
        plan_claims(root, remove=hidden_to_remove) if hidden_to_remove else None,
        plan_overlay_git(root, remove=overlay_to_remove) if overlay_to_remove else None,
    )


def plan_stale_removal(  # noqa: C901 - record-backed and record-less leftovers
    *,
    profile: str,
    target: Path,
    config_path: Path,
    owner_id: uuid.UUID | None = None,
) -> ProjectStaleRemovalPlan | None:
    """Plan dropping private state whose injection can no longer be removed.

    Covers a record whose directory vanished or was replaced, and claims whose
    record was lost. Returns ``None`` for an intact injection or when nothing
    is left. Project files are never touched: their identity is unverifiable.
    A caller that already holds the config identity lock passes ``owner_id``.
    """
    lexical = Path(os.path.normpath(target.expanduser().absolute()))
    root = lexical.resolve()
    canonical_config_path = config_path.resolve(strict=True)
    git_dir: Path | None = None
    live: os.stat_result | None = None
    if root != lexical and manifest_path(lexical, profile).exists():
        # The recorded directory is gone: its path now leads elsewhere.
        root = lexical
    elif root.is_dir():
        root, git_dir, live = _verified_project_target(root)
    claim_git_dir = str(git_dir) if git_dir is not None else None
    state_path = manifest_path(root, profile)
    store = OwnershipStore()
    if state_path.exists():
        # Dropping a stale record needs only its address, whatever its format.
        raw, _payload = _read_record_document(state_path)
        if raw["profile"] != profile or raw["target"] != str(root):
            return None
        stale = _stale_record_claims(raw, live)
        if stale is None:
            return None
        if raw["config_root"] != str(canonical_config_path.parent) or (
            "config_path" in raw and raw["config_path"] != str(canonical_config_path)
        ):
            raise SetforgeError(
                "project injection state belongs to a different config manifest"
            )
        try:
            owner_id = uuid.UUID(str(raw["config_owner_id"]))
        except ValueError as exc:
            raise SetforgeError(
                "project injection state has an invalid owner identity"
            ) from exc
        reason, resources = stale
        candidates = tuple(store.read(resource) for resource in resources)
        relatives = tuple(resource.coordinate for resource in resources)
        recorded_git_dir = raw["git_dir"]
        claim_git_dir = recorded_git_dir if isinstance(recorded_git_dir, str) else None
        manifest: Path | None = state_path
    else:
        if live is None:
            return None
        if owner_id is None:
            try:
                owner_id = read_owner_id(canonical_config_path.parent)
            except SetforgeError:
                return None
        reason = "the injection record is missing"
        scope = _resource_id(live.st_dev, live.st_ino, Path("scope")).scope
        candidates = tuple(
            claim
            for claim in store.list_claims()
            if claim.resource_id.provider == "project-profile"
            and claim.resource_id.scope == scope
        )
        manifest = None
    claims = tuple(
        claim
        for claim in candidates
        if claim is not None
        and claim.lifecycle is ClaimLifecycle.CLAIMED
        and claim.owner_id == owner_id
        and len(claim.declaration_refs) == 1
        and claim.declaration_refs[0].startswith(f"project-profile:{profile}:")
        and claim.locator == str(root / claim.resource_id.coordinate)
    )
    if manifest is None:
        if not claims:
            return None
        relatives = tuple(claim.resource_id.coordinate for claim in claims)
    visibility_plan, overlay_git_plan = (
        _stale_git_plans(root, Path(claim_git_dir), profile, relatives)
        if git_dir is not None and claim_git_dir is not None
        else (None, None)
    )
    return ProjectStaleRemovalPlan(
        profile=profile,
        target=root,
        reason=reason,
        config_path=canonical_config_path,
        manifest_path=manifest,
        owner_id=owner_id,
        claims=claims,
        overlay_paths=tuple(
            path
            for relative in relatives
            if (path := overlay_path(root, Path(relative))).exists()
        ),
        visibility_plan=visibility_plan,
        overlay_git_plan=overlay_git_plan,
    )


def apply_stale_removal(plan: ProjectStaleRemovalPlan) -> None:
    """Release the claims and retire the private state of one stale injection."""
    operation_profile = f"project-{_injection_key(plan.target, plan.profile)}"
    config_root = plan.config_path.parent
    with mutation_locks(
        resources=True,
        config_identity_dir=resolve_owner_common_dir(config_root),
        config_dir=config_root,
        target_roots=(
            (plan.target,)
            if plan.target.is_dir() and plan.target.resolve() == plan.target
            else ()
        ),
        profile=operation_profile,
    ) as guards:
        _require_config_owner(guards, config_root, plan.owner_id)
        fresh = plan_stale_removal(
            profile=plan.profile,
            target=plan.target,
            config_path=plan.config_path,
            owner_id=plan.owner_id,
        )
        if fresh != plan:
            raise SetforgeError("project removal plan changed before apply; retry")
        store = OwnershipStore()
        paths = (
            *((plan.manifest_path,) if plan.manifest_path is not None else ()),
            *plan.overlay_paths,
            *(store.claim_path(claim.resource_id) for claim in plan.claims),
            *_exclude_paths(plan.visibility_plan),
            *_overlay_git_paths(plan.overlay_git_plan),
        )
        with _project_transaction(
            command="project-remove",
            profile=operation_profile,
            config_dir=config_root,
            paths=paths,
            checkpoint="retire-stale-project-state",
            recovery="restore the manifest, ownership claims, and Git state",
        ):
            for claim in plan.claims:
                store.release_locked(
                    claim.resource_id,
                    expected_owner=plan.owner_id,
                    expected_generation=claim.generation,
                )
            if plan.visibility_plan is not None:
                apply_claims(plan.visibility_plan)
            if plan.overlay_git_plan is not None:
                apply_overlay_git(plan.overlay_git_plan)
            for overlay_state in plan.overlay_paths:
                overlay_state.unlink()
            if plan.manifest_path is not None:
                plan.manifest_path.unlink()
