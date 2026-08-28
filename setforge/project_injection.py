"""Reversible project-profile injection into one verified Git worktree."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from setforge import atomicio, operations
from setforge.config import (
    ProjectVisibility,
    ResolvedProjectFile,
    ResolvedProjectProfile,
)
from setforge.errors import SetforgeError
from setforge.git_overlay import (
    OverlayClaim,
    OverlayGitPlan,
    apply_overlay_git,
    overlay_claim_id,
    plan_overlay_git,
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
    ClaimLifecycle,
    OwnershipClaim,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    ResourceId,
    ResourceScope,
    load_or_create_owner_id_locked,
    read_owner_id_locked,
    resolve_owner_common_dir,
)
from setforge.project_overlay import (
    ProjectOverlay,
    build_overlay,
    clean_content,
    overlay_path,
    read_overlay,
    write_overlay,
)
from setforge.transitions import state_root

_MANIFEST_SCHEMA = 2
_LEGACY_MANIFEST_SCHEMA = 1


class ProjectFileAction(StrEnum):
    """One fully preflighted destination effect."""

    CREATE = "create"
    RETAIN = "retain-identical"
    REPLACE = "replace-untracked"
    OVERLAY = "overlay-tracked"


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
    no_op: bool = False


@dataclass(frozen=True, slots=True)
class ProjectRemovePlan:
    """A drift-validated removal plan backed by one durable manifest."""

    profile: str
    target: Path
    manifest_path: Path
    owner_id: uuid.UUID
    files: tuple[ProjectFilePlan, ...]
    created_parents: tuple[Path, ...]
    visibility_plan: VisibilityPlan | None
    overlay_git_plan: OverlayGitPlan | None


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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


def _run_git(
    target: Path, args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
        "LANG": "C",
        "LC_ALL": "C",
    }
    return subprocess.run(
        ["git", "-C", str(target), *args],
        check=check,
        text=True,
        capture_output=True,
        timeout=30,
        env=environment,
    )


def _verified_git_worktree(path: Path) -> tuple[Path, Path, os.stat_result]:
    lexical = path.expanduser().absolute()
    try:
        resolved = lexical.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise SetforgeError(
            f"project target cannot be resolved: {lexical}: {exc}"
        ) from exc
    if lexical != resolved:
        raise SetforgeError(
            f"project target must not use a symlink or alias path: {lexical}"
        )
    if not resolved.is_dir():
        raise SetforgeError(f"project target is not a directory: {resolved}")
    try:
        top = Path(
            _run_git(resolved, ["rev-parse", "--show-toplevel"]).stdout.strip()
        ).resolve()
        git_dir_raw = _run_git(resolved, ["rev-parse", "--git-dir"]).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetforgeError(
            f"project target must be an existing Git worktree root in G2: {resolved}"
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
    if lexical != resolved:
        raise SetforgeError(
            f"project target must not use a symlink or alias path: {lexical}"
        )
    if not resolved.is_dir():
        raise SetforgeError(f"project target is not a directory: {resolved}")
    result = _run_git(resolved, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        if (resolved / ".git").exists():
            detail = result.stderr.strip() or "unknown Git error"
            raise SetforgeError(f"project target has invalid Git metadata: {detail}")
        return resolved, None, resolved.stat()
    return _verified_git_worktree(resolved)


def _is_tracked(target: Path, relative: Path) -> bool:
    result = _run_git(
        target,
        ["ls-files", "--error-unmatch", "--", relative.as_posix()],
        check=False,
    )
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
        break
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


def plan_injection(
    *,
    profile: str,
    target: Path,
    config_root: Path,
    config_path: Path | None = None,
    resolved: ResolvedProjectProfile,
    visibility: ProjectVisibility,
) -> ProjectInjectionPlan:
    """Build a complete injection plan without changing target or state."""
    root, git_dir, target_stat = _verified_project_target(target)
    state_path = manifest_path(root, profile)
    files = tuple(
        _plan_file(root, item, git=git_dir is not None) for item in resolved.files
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
        for item in files
        if git_dir is not None and item.action is not ProjectFileAction.OVERLAY
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
        for item in files
        if git_dir is not None and item.action is ProjectFileAction.OVERLAY
    )
    if git_dir is None:
        visibility_plan = None
        overlay_git_plan = None
    else:
        _require_compatible_visibility(
            target=root,
            manifest=state_path,
            visibility=visibility,
            claims=visibility_claims,
        )
        visibility_plan = plan_claims(
            root,
            add=visibility_claims if visibility is ProjectVisibility.HIDDEN else (),
        )
        overlay_git_plan = plan_overlay_git(root, add=overlay_claims)
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
    if state_path.exists():
        _validate_existing_injection(plan)
        return ProjectInjectionPlan(
            profile=plan.profile,
            target=plan.target,
            target_device=plan.target_device,
            target_inode=plan.target_inode,
            git_dir=plan.git_dir,
            visibility=plan.visibility,
            config_root=plan.config_root,
            config_path=plan.config_path,
            files=plan.files,
            manifest_path=plan.manifest_path,
            visibility_plan=plan.visibility_plan,
            overlay_git_plan=plan.overlay_git_plan,
            no_op=True,
        )
    return plan


def resolve_injection_plan(
    plan: ProjectInjectionPlan,
    *,
    auto: str | None,
    interactive: bool,
) -> ProjectInjectionPlan | None:
    """Resolve initial tracked-file collisions without mutating state."""
    from setforge.project_sync import legacy_two_way_merge
    from setforge.reconcile.merge_model import Clean, Conflict, MergeResult
    from setforge.reconcile.types import FileId
    from setforge.ui.primitives import CANCEL

    resolved_files: list[ProjectFilePlan] = []
    for item in plan.files:
        if item.action is not ProjectFileAction.OVERLAY:
            resolved_files.append(item)
            continue
        assert item.previous_payload is not None
        result = legacy_two_way_merge(item.previous_payload, item.source_payload)
        if result.clean:
            merged = result.merged()
        elif auto is not None:
            if auto not in {"keep-live", "use-profile"}:
                raise SetforgeError(f"unknown project injection resolution: {auto}")
            segments = tuple(
                segment
                if isinstance(segment, Clean)
                else Clean(segment.ours if auto == "keep-live" else segment.theirs)
                for segment in result.segments
            )
            merged = MergeResult(segments).merged()
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


def _require_compatible_visibility(
    *,
    target: Path,
    manifest: Path,
    visibility: ProjectVisibility,
    claims: tuple[VisibilityClaim, ...],
) -> None:
    """Refuse repository-common hidden/tracked ambiguity before mutation."""
    exclude_path, _, _, hidden_claims = read_claims(target)
    hidden_paths = {claim.relative_path for claim in hidden_claims}
    requested_paths = {claim.relative_path for claim in claims}
    if visibility is ProjectVisibility.TRACKED and requested_paths & hidden_paths:
        conflict = sorted(requested_paths & hidden_paths)[0]
        raise SetforgeError(
            f"project visibility conflicts across linked worktrees for {conflict}: "
            "the repository already has a hidden claim"
        )
    records = state_root() / "project-injections"
    if not records.is_dir():
        return
    for path in records.glob("*.json"):
        if path == manifest:
            continue
        try:
            raw = _load_manifest(path)
            other_target = Path(str(raw["target"]))
            if info_exclude_path(other_target) != exclude_path:
                continue
            other_visibility = ProjectVisibility(str(raw["visibility"]))
            raw_files = raw["files"]
            assert isinstance(raw_files, list)
            other_paths = {
                str(item["destination"])
                for item in raw_files
                if isinstance(item, dict) and "destination" in item
            }
        except (AssertionError, OSError, SetforgeError, ValueError) as exc:
            raise SetforgeError(
                f"cannot validate sibling project visibility record: {path}"
            ) from exc
        conflicts = requested_paths & other_paths
        if conflicts and other_visibility is not visibility:
            conflict = sorted(conflicts)[0]
            raise SetforgeError(
                f"project visibility conflicts across linked worktrees for {conflict}: "
                f"recorded {other_visibility.value}, requested {visibility.value}"
            )


def _resource_id(target: Path, relative: Path) -> ResourceId:
    return ResourceId(
        kind="file",
        provider="project-profile",
        coordinate=relative.as_posix(),
        scope=ResourceScope.target_root(target),
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


def _load_manifest_payload(path: Path) -> tuple[dict[str, object], bytes]:
    """Load and validate a manifest while retaining its exact bound bytes."""
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
    if raw["schema"] == _MANIFEST_SCHEMA:
        required.add("config_path")
    if set(raw) != required or not isinstance(raw["files"], list):
        raise SetforgeError(f"project injection state has invalid fields: {path}")
    return raw, payload


def _load_manifest(path: Path) -> dict[str, object]:
    raw, _payload = _load_manifest_payload(path)
    return raw


def _validate_existing_injection(plan: ProjectInjectionPlan) -> None:
    raw = _load_manifest(plan.manifest_path)
    if (
        raw["profile"] != plan.profile
        or raw["target"] != str(plan.target)
        or raw["target_device"] != plan.target_device
        or raw["target_inode"] != plan.target_inode
        or raw["git_dir"] != (str(plan.git_dir) if plan.git_dir is not None else None)
        or raw["config_root"] != str(plan.config_root)
        or (
            raw["schema"] == _MANIFEST_SCHEMA
            and raw["config_path"] != str(plan.config_path)
        )
        or raw["visibility"] != plan.visibility.value
    ):
        raise SetforgeError(
            "project injection request differs from recorded state; use project "
            "sync when G4 is available"
        )
    raw_files = raw["files"]
    assert isinstance(raw_files, list)
    expected = [
        (item.relative_destination.as_posix(), item.source_digest, item.source_mode)
        for item in plan.files
    ]
    observed = [
        (item.get("destination"), item.get("source_digest"), item.get("applied_mode"))
        for item in raw_files
        if isinstance(item, dict)
    ]
    if observed != expected:
        raise SetforgeError(
            "project profile or source changed since injection; use project sync "
            "when G4 is available"
        )
    for item, record in zip(plan.files, raw_files, strict=True):
        assert isinstance(record, dict)
        try:
            info = item.destination.lstat()
        except FileNotFoundError as exc:
            raise SetforgeError(
                f"injected project file is missing: {item.destination}"
            ) from exc
        live_payload = item.destination.read_bytes()
        valid_payload = _sha256(live_payload) == str(record.get("applied_digest"))
        if record.get("action") == ProjectFileAction.OVERLAY.value:
            overlay = read_overlay(plan.target, item.relative_destination)
            if overlay is None:
                valid_payload = False
            else:
                valid_payload = _sha256(overlay.local) == str(
                    record.get("applied_digest")
                )
                if valid_payload:
                    clean_content(overlay, live_payload)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or not valid_payload
            or stat.S_IMODE(info.st_mode) != int(record.get("applied_mode", -1))
        ):
            raise SetforgeError(
                f"injected project file has drifted: {item.destination}"
            )


def _require_guards(guards: MutationLockGuards, target: Path) -> None:
    if len(guards.targets) != 1 or guards.targets[0].target != target:
        raise SetforgeError("project target lock binding is invalid")
    guards.verify_targets()


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
    with _relative_parent(guard, relative, create=True) as parent_fd:
        atomicio.atomic_write_bytes_at(parent_fd, relative.name, payload, mode=mode)


def _unlink_project_file(guard: TargetLockGuard, relative: Path) -> None:
    with _relative_parent(guard, relative, create=False) as parent_fd:
        os.unlink(relative.name, dir_fd=parent_fd)
        os.fsync(parent_fd)


def _remove_created_parent(guard: TargetLockGuard, relative: Path) -> None:
    with _relative_parent(guard, relative, create=False) as parent_fd:
        try:
            os.rmdir(relative.name, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno not in {39, 66}:
                raise
        else:
            os.fsync(parent_fd)


def apply_injection(  # noqa: C901 - one fail-closed journaled transaction
    plan: ProjectInjectionPlan, *, mutate_visibility: bool = True
) -> bool:
    """Apply a previously confirmed plan; return False for an exact no-op."""
    operation_profile = f"project-{_injection_key(plan.target, plan.profile)}"
    with mutation_locks(
        resources=True,
        config_identity_dir=resolve_owner_common_dir(plan.config_root),
        config_dir=plan.config_root,
        target_roots=(plan.target,),
        profile=operation_profile,
    ) as guards:
        _require_guards(guards, plan.target)
        fresh = plan_injection(
            profile=plan.profile,
            target=plan.target,
            config_root=plan.config_root,
            resolved=_resolved_from_plan(plan),
            visibility=plan.visibility,
        )
        unresolved_files = tuple(
            replace(item, applied_payload=None, overlay=None)
            if item.action is ProjectFileAction.OVERLAY
            else item
            for item in plan.files
        )
        if fresh != replace(plan, files=unresolved_files):
            raise SetforgeError("project injection plan changed before apply; retry")
        identity = guards.config_identity
        if identity is None:
            raise SetforgeError("project config identity lock is missing")
        owner_id = (
            read_owner_id_locked(plan.config_root, identity.directory_fd)
            if fresh.no_op
            else load_or_create_owner_id_locked(
                plan.config_root, identity.directory_fd, uuid.uuid4()
            )
        )
        store = OwnershipStore()
        resources = tuple(
            _resource_id(plan.target, item.relative_destination) for item in plan.files
        )
        claims = tuple(store.read(resource) for resource in resources)
        if fresh.no_op:
            if any(
                claim is None
                or not _claim_matches_plan(
                    claim,
                    resource=resource,
                    owner_id=owner_id,
                    profile=plan.profile,
                    item=item,
                    lifecycle=ClaimLifecycle.CLAIMED,
                )
                for item, resource, claim in zip(
                    plan.files, resources, claims, strict=True
                )
            ):
                raise SetforgeError(
                    "project injection ownership state is missing or mismatched"
                )
            if (
                fresh.visibility_plan is None
                or not fresh.visibility_plan.changed
                or not mutate_visibility
            ):
                return False
            visibility_paths: tuple[Path, ...] = (fresh.visibility_plan.exclude_path,)
            journal = operations.prepare(
                command="project-inject",
                profile=operation_profile,
                config_dir=plan.config_root,
                resources_lock=True,
                command_line=("project", "inject", plan.profile, str(plan.target)),
                paths=visibility_paths,
                path_guards=capture_parent_path_guards(visibility_paths),
            )
            with operations.recover_on_error(operation_profile, "project-inject"):
                journal = operations.begin_checkpoint(
                    journal,
                    name="activate-project-visibility",
                    kind=operations.CheckpointKind.REVERSIBLE,
                    recovery="restore the repository-private Git visibility file",
                    paths=visibility_paths,
                    restore_state=False,
                    restore_transitions=False,
                )
                apply_claims(fresh.visibility_plan)
                journal = operations.finish_checkpoint(journal)
                operations.complete(journal)
            return True
        if any(
            claim is not None
            and not _claim_matches_plan(
                claim,
                resource=resource,
                owner_id=owner_id,
                profile=plan.profile,
                item=item,
                lifecycle=ClaimLifecycle.RELEASED,
            )
            for item, resource, claim in zip(plan.files, resources, claims, strict=True)
        ):
            raise SetforgeError(
                "a project destination already has an active ownership claim"
            )
        visibility_paths = (
            (plan.visibility_plan.exclude_path,)
            if plan.visibility_plan is not None
            else ()
        )
        overlay_paths = tuple(
            overlay_path(plan.target, item.relative_destination)
            for item in plan.files
            if item.action is ProjectFileAction.OVERLAY
        )
        overlay_git_paths = (
            (plan.overlay_git_plan.config_path, plan.overlay_git_plan.attributes_path)
            if plan.overlay_git_plan is not None
            else ()
        )
        paths = (
            *(item.destination for item in plan.files),
            *overlay_paths,
            *overlay_git_paths,
            plan.manifest_path,
            *(store.claim_path(resource) for resource in resources),
            *visibility_paths,
        )
        path_guards = capture_parent_path_guards(paths)
        journal = operations.prepare(
            command="project-inject",
            profile=operation_profile,
            config_dir=plan.config_root,
            resources_lock=True,
            command_line=("project", "inject", plan.profile, str(plan.target)),
            paths=paths,
            path_guards=path_guards,
        )
        with operations.recover_on_error(operation_profile, "project-inject"):
            journal = operations.begin_checkpoint(
                journal,
                name="materialize-project-files-and-state",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery="restore project files, manifest, and ownership claims",
                paths=paths,
                restore_state=False,
                restore_transitions=False,
            )
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
            journal = operations.finish_checkpoint(journal)
            operations.complete(journal)
    return True


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
    *, profile: str, target: Path, config_root: Path
) -> ProjectRemovePlan:
    """Load and drift-check the exact injection to remove."""
    root, git_dir, target_stat = _verified_project_target(target)
    state_path = manifest_path(root, profile)
    raw = _load_manifest(state_path)
    if (
        raw["profile"] != profile
        or raw["target"] != str(root)
        or raw["target_device"] != target_stat.st_dev
        or raw["target_inode"] != target_stat.st_ino
        or raw["git_dir"] != (str(git_dir) if git_dir is not None else None)
        or raw["config_root"] != str(config_root.resolve(strict=True))
    ):
        raise SetforgeError(
            "project injection state does not match this target or config checkout"
        )
    try:
        owner_id = uuid.UUID(str(raw["config_owner_id"]))
    except (ValueError, TypeError) as exc:
        raise SetforgeError(
            "project injection state has an invalid owner identity"
        ) from exc
    files: list[ProjectFilePlan] = []
    all_parents: set[Path] = set()
    destinations: set[str] = set()
    file_ids: set[str] = set()
    raw_files = raw["files"]
    assert isinstance(raw_files, list)
    for entry in raw_files:
        if not isinstance(entry, dict):
            raise SetforgeError("project injection state has an invalid file record")
        expected_fields = {
            "action",
            "applied_digest",
            "applied_mode",
            "created_parents",
            "declaring_profile",
            "destination",
            "file_id",
            "previous_mode",
            "previous_payload",
            "source",
            "source_digest",
        }
        if raw["schema"] == _MANIFEST_SCHEMA:
            expected_fields |= {
                "applied_payload",
                "upstream_mode",
                "upstream_payload",
            }
        if set(entry) != expected_fields:
            raise SetforgeError("project injection state has invalid file fields")
        try:
            relative = Path(str(entry["destination"]))
            file_id = str(entry["file_id"])
            if (
                relative.is_absolute()
                or relative == Path()
                or ".." in relative.parts
                or relative.as_posix() in destinations
                or file_id in file_ids
            ):
                raise ValueError
            destinations.add(relative.as_posix())
            file_ids.add(file_id)
            destination = root / relative
            action = ProjectFileAction(str(entry["action"]))
            source_digest = str(entry["source_digest"])
            if raw["schema"] == _MANIFEST_SCHEMA:
                applied_payload_raw = entry["applied_payload"]
                applied_digest_raw = entry["applied_digest"]
                applied_mode_raw = entry["applied_mode"]
                applied_payload = (
                    base64.b64decode(str(applied_payload_raw), validate=True)
                    if applied_payload_raw is not None
                    else None
                )
                applied_digest = (
                    str(applied_digest_raw) if applied_digest_raw is not None else None
                )
                applied_mode = (
                    int(applied_mode_raw) if applied_mode_raw is not None else None
                )
                upstream_payload = base64.b64decode(
                    str(entry["upstream_payload"]), validate=True
                )
                upstream_mode = int(entry["upstream_mode"])
            else:
                applied_digest = str(entry["applied_digest"])
                applied_mode = int(entry["applied_mode"])
                applied_payload = None
            previous_mode_raw = entry["previous_mode"]
            previous_mode = (
                int(previous_mode_raw) if previous_mode_raw is not None else None
            )
            previous_payload_raw = entry["previous_payload"]
            previous_payload = (
                base64.b64decode(str(previous_payload_raw), validate=True)
                if previous_payload_raw is not None
                else None
            )
            parents = tuple(
                root / Path(str(value)) for value in entry["created_parents"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SetforgeError(
                "project injection state has an invalid file record"
            ) from exc
        try:
            destination.relative_to(root)
            _created_parents(root, destination)
            info = destination.lstat()
        except ValueError as exc:
            raise SetforgeError(
                f"injected project file is unsafe: {destination}"
            ) from exc
        except FileNotFoundError:
            info = None
        for parent in parents:
            try:
                relative_parent = parent.relative_to(root)
            except ValueError as exc:
                raise SetforgeError(
                    "project injection state has an escaping parent"
                ) from exc
            if (
                parent == root
                or parent not in destination.parents
                or ".." in relative_parent.parts
            ):
                raise SetforgeError("project injection state has an invalid parent")
        live_payload = destination.read_bytes() if info is not None else None
        baseline_absent = previous_payload is None and previous_mode is None
        baseline_complete = previous_payload is not None and previous_mode is not None
        applied_absent = (
            applied_payload is None and applied_digest is None and applied_mode is None
        )
        applied_complete = (
            applied_payload is not None
            and applied_digest is not None
            and applied_mode is not None
        )
        if (
            (
                raw["schema"] == _LEGACY_MANIFEST_SCHEMA
                and applied_digest != source_digest
            )
            or (
                raw["schema"] == _MANIFEST_SCHEMA
                and (
                    (not applied_absent and not applied_complete)
                    or (
                        applied_payload is not None
                        and _sha256(applied_payload) != applied_digest
                    )
                    or _sha256(upstream_payload) != source_digest
                    or upstream_mode < 0
                )
            )
            or (action is ProjectFileAction.CREATE and not baseline_absent)
            or (action is not ProjectFileAction.CREATE and not baseline_complete)
            or (
                raw["schema"] == _LEGACY_MANIFEST_SCHEMA
                and action is ProjectFileAction.RETAIN
                and (previous_payload != live_payload or previous_mode != applied_mode)
            )
            or (
                raw["schema"] == _LEGACY_MANIFEST_SCHEMA
                and action is ProjectFileAction.REPLACE
                and previous_payload == live_payload
                and previous_mode == applied_mode
            )
        ):
            raise SetforgeError(
                "project injection state has an inconsistent file record"
            )
        overlay: ProjectOverlay | None = None
        live_matches_absent = info is None and applied_absent
        live_matches_present = (
            info is not None
            and not stat.S_ISLNK(info.st_mode)
            and stat.S_ISREG(info.st_mode)
            and live_payload is not None
            and applied_digest is not None
            and _sha256(live_payload) == applied_digest
            and stat.S_IMODE(info.st_mode) == applied_mode
        )
        if action is ProjectFileAction.OVERLAY:
            if (
                raw["schema"] != _MANIFEST_SCHEMA
                or info is None
                or stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or live_payload is None
                or applied_payload is None
                or previous_payload is None
                or applied_mode is None
                or stat.S_IMODE(info.st_mode) != applied_mode
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
            clean_content(overlay, live_payload)
            live_matches_present = True
        if not live_matches_absent and not live_matches_present:
            raise SetforgeError(f"injected project file has drifted: {destination}")
        if raw["schema"] == _MANIFEST_SCHEMA:
            claim_payload = upstream_payload
            claim_mode = upstream_mode
        else:
            if live_payload is None or applied_mode is None:
                raise SetforgeError(
                    "legacy project injection has incomplete applied state"
                )
            claim_payload = live_payload
            claim_mode = applied_mode
        all_parents.update(parents)
        files.append(
            ProjectFilePlan(
                file_id=file_id,
                declaring_profile=str(entry["declaring_profile"]),
                source=Path(str(entry["source"])),
                destination=destination,
                relative_destination=relative,
                source_payload=claim_payload,
                source_mode=claim_mode,
                source_digest=source_digest,
                applied_payload=applied_payload,
                action=action,
                previous_payload=previous_payload,
                previous_mode=previous_mode,
                created_parents=parents,
                overlay=overlay,
            )
        )
    try:
        visibility = ProjectVisibility(str(raw["visibility"]))
    except ValueError as exc:
        raise SetforgeError("project injection state has invalid visibility") from exc
    hidden_to_remove: tuple[VisibilityClaim, ...] = ()
    if visibility is ProjectVisibility.HIDDEN and git_dir is not None:
        expected_claims = tuple(
            VisibilityClaim(
                claim_id=claim_id(
                    target_git_dir=git_dir,
                    profile=profile,
                    relative_path=item.relative_destination.as_posix(),
                ),
                relative_path=item.relative_destination.as_posix(),
            )
            for item in files
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
                git_dir=git_dir,
                profile=profile,
                relative_path=item.relative_destination.as_posix(),
            ),
            item.relative_destination.as_posix(),
        )
        for item in files
        if git_dir is not None and item.action is ProjectFileAction.OVERLAY
    )
    overlay_git_plan = (
        plan_overlay_git(root, remove=overlay_to_remove)
        if git_dir is not None and overlay_to_remove
        else None
    )
    return ProjectRemovePlan(
        profile=profile,
        target=root,
        manifest_path=state_path,
        owner_id=owner_id,
        files=tuple(files),
        created_parents=tuple(
            sorted(all_parents, key=lambda path: len(path.parts), reverse=True)
        ),
        visibility_plan=visibility_plan,
        overlay_git_plan=overlay_git_plan,
    )


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
            if item.overlay is None:
                raise SetforgeError("tracked project overlay state is missing")
            overlay_live_payload = item.destination.read_bytes()
            restored = clean_content(item.overlay, overlay_live_payload)
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


def apply_removal(plan: ProjectRemovePlan, *, config_root: Path) -> None:
    """Restore one drift-free injection and retire its private state."""
    operation_profile = f"project-{_injection_key(plan.target, plan.profile)}"
    with mutation_locks(
        resources=True,
        config_identity_dir=resolve_owner_common_dir(config_root),
        config_dir=config_root,
        target_roots=(plan.target,),
        profile=operation_profile,
    ) as guards:
        _require_guards(guards, plan.target)
        fresh = plan_removal(
            profile=plan.profile, target=plan.target, config_root=config_root
        )
        if fresh != plan:
            raise SetforgeError("project removal plan changed before apply; retry")
        identity = guards.config_identity
        if identity is None:
            raise SetforgeError("project config identity lock is missing")
        actual_owner = read_owner_id_locked(config_root, identity.directory_fd)
        if actual_owner != plan.owner_id:
            raise SetforgeError(
                "project injection belongs to a different config checkout"
            )
        store = OwnershipStore()
        resources = tuple(
            _resource_id(plan.target, item.relative_destination) for item in plan.files
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
        visibility_paths = (
            (plan.visibility_plan.exclude_path,)
            if plan.visibility_plan is not None
            else ()
        )
        overlay_paths = tuple(
            overlay_path(plan.target, item.relative_destination)
            for item in plan.files
            if item.action is ProjectFileAction.OVERLAY
        )
        overlay_git_paths = (
            (plan.overlay_git_plan.config_path, plan.overlay_git_plan.attributes_path)
            if plan.overlay_git_plan is not None
            else ()
        )
        paths = (
            *(item.destination for item in plan.files),
            *overlay_paths,
            *overlay_git_paths,
            *plan.created_parents,
            plan.manifest_path,
            *(store.claim_path(resource) for resource in resources),
            *visibility_paths,
        )
        path_guards = capture_parent_path_guards(paths)
        journal = operations.prepare(
            command="project-remove",
            profile=operation_profile,
            config_dir=config_root,
            resources_lock=True,
            command_line=("project", "remove", plan.profile, str(plan.target)),
            paths=paths,
            path_guards=path_guards,
        )
        with operations.recover_on_error(operation_profile, "project-remove"):
            journal = operations.begin_checkpoint(
                journal,
                name="restore-project-files-and-retire-state",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery="restore injected files, manifest, and ownership claims",
                paths=paths,
                restore_state=False,
                restore_transitions=False,
            )
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
            journal = operations.finish_checkpoint(journal)
            operations.complete(journal)
