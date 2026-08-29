"""Truthful inspection and per-file visibility for project injections."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from setforge import atomicio, operations
from setforge.config import ProjectVisibility
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
    plan_claims,
    read_claims,
)
from setforge.locking import mutation_locks
from setforge.orphan_scan import capture_parent_path_guards
from setforge.project_injection import (
    _MANIFEST_SCHEMA,
    ProjectFileAction,
    _load_manifest_payload,
    _require_compatible_visibility,
    _sha256,
    _verified_project_target,
    plan_removal,
)
from setforge.project_overlay import clean_content, read_overlay
from setforge.project_sync import (
    RecordedProjectInjection,
    StoredProjectFile,
    _stored_files,
)
from setforge.transitions import state_root


class ProjectFileVisibility(StrEnum):
    """User-facing actual visibility of one injected destination."""

    HIDDEN = "hidden"
    TRACKED = "tracked"
    TRACKED_OVERLAY = "tracked-overlay"
    NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True, slots=True)
class ProjectListFile:
    """One listed destination or one record-level diagnostic."""

    record: Path
    target: Path | None
    profile: str | None
    destination: Path | None
    visibility: ProjectFileVisibility | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectVisibilityPlan:
    """One exact-state-bound per-file visibility transition."""

    manifest_path: Path
    manifest_before: bytes
    manifest_after: bytes
    target: Path
    profile: str
    destination: Path
    current: ProjectFileVisibility
    requested: ProjectVisibility
    config_root: Path
    git_dir: Path | None
    visibility_plan: VisibilityPlan | None
    overlay_git_plan: OverlayGitPlan | None
    index_path: Path | None
    remove_from_index: bool

    @property
    def changed(self) -> bool:
        """Return whether the plan has any durable effect."""
        return (
            self.manifest_before != self.manifest_after
            or self.remove_from_index
            or (self.visibility_plan is not None and self.visibility_plan.changed)
            or (self.overlay_git_plan is not None and self.overlay_git_plan.changed)
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
    try:
        return subprocess.run(
            ["git", "-C", str(target), *args],
            check=check,
            text=True,
            capture_output=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise SetforgeError(f"cannot inspect project visibility: {detail}") from exc


def _records() -> tuple[Path, ...]:
    root = state_root() / "project-injections"
    return tuple(sorted(root.glob("*.json"))) if root.exists() else ()


def _file_visibility(
    raw: dict[str, object], entry: dict[str, object]
) -> ProjectVisibility:
    value = entry.get("visibility", raw.get("visibility"))
    try:
        return ProjectVisibility(str(value))
    except ValueError as exc:
        raise SetforgeError("project injection state has invalid visibility") from exc


def _entry_action(entry: dict[str, object]) -> ProjectFileAction:
    try:
        return ProjectFileAction(str(entry["action"]))
    except (KeyError, ValueError) as exc:
        raise SetforgeError("project injection state has invalid file action") from exc


def _manifest_schema(raw: dict[str, object]) -> int:
    value = raw.get("schema")
    if not isinstance(value, int):
        raise SetforgeError("project injection state has invalid schema")
    return value


def _ordinary_actual_visibility(
    *,
    target: Path,
    git_dir: Path,
    profile: str,
    relative: Path,
    declared: ProjectVisibility,
) -> tuple[ProjectFileVisibility, ProjectFileVisibility]:
    visibility_claim = VisibilityClaim(
        claim_id=claim_id(
            target_git_dir=git_dir,
            profile=profile,
            relative_path=relative.as_posix(),
        ),
        relative_path=relative.as_posix(),
    )
    current = {item.claim_id: item for item in read_claims(target)[3]}
    observed = current.get(visibility_claim.claim_id)
    if observed is not None and observed != visibility_claim:
        raise SetforgeError("Git visibility claim identity collides")
    actual = (
        ProjectFileVisibility.HIDDEN
        if observed is not None
        else ProjectFileVisibility.TRACKED
    )
    indexed = _run_git(
        target,
        ["ls-files", "--error-unmatch", "--", relative.as_posix()],
        check=False,
    )
    if indexed.returncode not in {0, 1}:
        raise SetforgeError("cannot classify project visibility index state")
    if observed is not None and indexed.returncode == 0:
        raise SetforgeError(
            f"hidden project file is present in the Git index: {relative}"
        )
    expected = (
        ProjectFileVisibility.HIDDEN
        if declared is ProjectVisibility.HIDDEN
        else ProjectFileVisibility.TRACKED
    )
    return actual, expected


def _actual_visibility(
    *,
    raw: dict[str, object],
    entry: dict[str, object],
    target: Path,
    profile: str,
    git_dir: Path | None,
    stored: StoredProjectFile | None = None,
    validate_declared: bool = True,
) -> ProjectFileVisibility:
    relative = Path(str(entry["destination"]))
    if relative.is_absolute() or relative == Path() or ".." in relative.parts:
        raise SetforgeError("project injection state has invalid destination")
    destination = target / relative
    info = destination.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SetforgeError(f"injected project file is not regular: {relative}")
    expected_mode = entry.get("applied_mode")
    if (
        not isinstance(expected_mode, int)
        or stat.S_IMODE(info.st_mode) != expected_mode
    ):
        raise SetforgeError(f"injected project file has drifted: {relative}")
    live_payload = destination.read_bytes()
    if git_dir is None:
        return ProjectFileVisibility.NOT_APPLICABLE
    declared = _file_visibility(raw, entry)
    action = _entry_action(entry)
    if action is ProjectFileAction.OVERLAY:
        overlay = read_overlay(target, relative)
        if overlay is None:
            raise SetforgeError(f"tracked project overlay is missing: {relative}")
        if stored is not None and (
            overlay.base != stored.previous_payload
            or overlay.local != stored.applied_payload
        ):
            raise SetforgeError(f"tracked project overlay has drifted: {relative}")
        clean_content(overlay, live_payload)
        overlay_claim = OverlayClaim(
            overlay_claim_id(
                git_dir=git_dir,
                profile=profile,
                relative_path=relative.as_posix(),
            ),
            relative.as_posix(),
        )
        probe = plan_overlay_git(target, add=(overlay_claim,))
        actual = (
            ProjectFileVisibility.TRACKED_OVERLAY
            if not probe.added
            else ProjectFileVisibility.TRACKED
        )
        expected = (
            ProjectFileVisibility.TRACKED_OVERLAY
            if declared is ProjectVisibility.HIDDEN
            else ProjectFileVisibility.TRACKED
        )
    else:
        expected_digest = entry.get("applied_digest")
        if (
            not isinstance(expected_digest, str)
            or _sha256(live_payload) != expected_digest
        ):
            raise SetforgeError(f"injected project file has drifted: {relative}")
        actual, expected = _ordinary_actual_visibility(
            target=target,
            git_dir=git_dir,
            profile=profile,
            relative=relative,
            declared=declared,
        )
    if validate_declared and actual is not expected:
        raise SetforgeError(
            f"recorded {declared.value} visibility does not match Git state "
            f"for {relative}"
        )
    return actual


def _validated_record(
    record: Path, raw: dict[str, object], payload: bytes
) -> tuple[RecordedProjectInjection, tuple[StoredProjectFile, ...]]:
    target = Path(str(raw["target"]))
    root, git_dir, target_info = _verified_project_target(target)
    if (
        str(root) != raw["target"]
        or raw["target_device"] != target_info.st_dev
        or raw["target_inode"] != target_info.st_ino
        or raw["git_dir"] != (str(git_dir) if git_dir is not None else None)
    ):
        raise SetforgeError("project target identity does not match the record")
    config_root = Path(str(raw["config_root"])).resolve(strict=True)
    config_path = (
        Path(str(raw["config_path"])).resolve(strict=True)
        if _manifest_schema(raw) >= 2
        else (config_root / "setforge.yaml").resolve(strict=True)
    )
    config_path.relative_to(config_root)
    injection = RecordedProjectInjection(
        profile=str(raw["profile"]),
        target=root,
        git_dir=git_dir,
        config_root=config_root,
        config_path=config_path,
        manifest_path=record,
        manifest_payload=payload,
        schema=_manifest_schema(raw),
    )
    return injection, _stored_files(injection)


def list_projects() -> tuple[ProjectListFile, ...]:
    """Return every injection file, retaining an error for each invalid record."""
    rows: list[ProjectListFile] = []
    for record in _records():
        target: Path | None = None
        profile: str | None = None
        try:
            raw, payload = _load_manifest_payload(record)
            injection, stored_files = _validated_record(record, raw, payload)
            target = injection.target
            profile = injection.profile
            git_dir = injection.git_dir
            entries = raw["files"]
            assert isinstance(entries, list)
            for entry, stored in zip(entries, stored_files, strict=True):
                if not isinstance(entry, dict):
                    raise SetforgeError("project injection state has an invalid file")
                relative = Path(str(entry["destination"]))
                try:
                    visibility = _actual_visibility(
                        raw=raw,
                        entry=entry,
                        target=target,
                        profile=profile,
                        git_dir=git_dir,
                        stored=stored,
                    )
                    error = None
                except (OSError, SetforgeError) as exc:
                    visibility = None
                    error = str(exc)
                rows.append(
                    ProjectListFile(
                        record=record,
                        target=target,
                        profile=profile,
                        destination=relative,
                        visibility=visibility,
                        error=error,
                    )
                )
        except (
            AssertionError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            SetforgeError,
        ) as exc:
            rows.append(
                ProjectListFile(
                    record=record,
                    target=target,
                    profile=profile,
                    destination=None,
                    visibility=None,
                    error=str(exc),
                )
            )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                str(row.target) if row.target is not None else "",
                row.profile or "",
                str(row.destination) if row.destination is not None else "",
                str(row.record),
            ),
        )
    )


def _index_path(target: Path) -> Path:
    value = _run_git(target, ["rev-parse", "--git-path", "index"]).stdout.strip()
    path = Path(value)
    if not path.is_absolute():
        path = target / path
    return path.resolve(strict=True)


def _render_manifest(
    raw: dict[str, object], *, destination: Path, requested: ProjectVisibility
) -> bytes:
    rendered = dict(raw)
    entries = raw["files"]
    assert isinstance(entries, list)
    new_entries: list[dict[str, object]] = []
    for value in entries:
        if not isinstance(value, dict):
            raise SetforgeError("project injection state has an invalid file")
        entry = dict(value)
        entry["visibility"] = (
            requested.value
            if entry.get("destination") == destination.as_posix()
            else _file_visibility(raw, entry).value
        )
        new_entries.append(entry)
    rendered["schema"] = _MANIFEST_SCHEMA
    rendered["files"] = new_entries
    return (json.dumps(rendered, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _plan_overlay_visibility(
    *,
    root: Path,
    git_dir: Path,
    profile: str,
    raw: dict[str, object],
    selected: dict[str, object],
    destination: Path,
    current: ProjectFileVisibility,
    requested: ProjectVisibility,
) -> OverlayGitPlan:
    additions: list[OverlayClaim] = []
    removals: list[OverlayClaim] = []
    entries = raw["files"]
    assert isinstance(entries, list)
    candidates = (
        [value for value in entries if isinstance(value, dict)]
        if _manifest_schema(raw) < _MANIFEST_SCHEMA
        else [selected]
    )
    for candidate in candidates:
        if _entry_action(candidate) is not ProjectFileAction.OVERLAY:
            continue
        relative = Path(str(candidate["destination"]))
        claim = OverlayClaim(
            overlay_claim_id(
                git_dir=git_dir,
                profile=profile,
                relative_path=relative.as_posix(),
            ),
            relative.as_posix(),
        )
        desired = (
            requested if relative == destination else _file_visibility(raw, candidate)
        )
        legacy = _manifest_schema(raw) < _MANIFEST_SCHEMA
        if desired is ProjectVisibility.HIDDEN and (
            legacy or current is ProjectFileVisibility.TRACKED
        ):
            additions.append(claim)
        elif desired is ProjectVisibility.TRACKED and (
            legacy or current is ProjectFileVisibility.TRACKED_OVERLAY
        ):
            removals.append(claim)
    return plan_overlay_git(root, add=tuple(additions), remove=tuple(removals))


def _plan_ordinary_visibility(
    *,
    root: Path,
    git_dir: Path,
    profile: str,
    destination: Path,
    requested: ProjectVisibility,
) -> tuple[VisibilityPlan, Path | None, bool]:
    claim = VisibilityClaim(
        claim_id(
            target_git_dir=git_dir,
            profile=profile,
            relative_path=destination.as_posix(),
        ),
        destination.as_posix(),
    )
    current_claims = read_claims(root)[3]
    own = next(
        (item for item in current_claims if item.claim_id == claim.claim_id), None
    )
    siblings = tuple(
        item
        for item in current_claims
        if item.relative_path == destination.as_posix()
        and item.claim_id != claim.claim_id
    )
    if requested is ProjectVisibility.TRACKED and siblings:
        raise SetforgeError(
            "Git visibility remains hidden by another linked-worktree claim"
        )
    visibility_plan = plan_claims(
        root,
        add=(claim,) if requested is ProjectVisibility.HIDDEN and own is None else (),
        remove=(claim,)
        if requested is ProjectVisibility.TRACKED and own is not None
        else (),
    )
    tracked = _run_git(
        root,
        ["ls-files", "--error-unmatch", "--", destination.as_posix()],
        check=False,
    )
    if tracked.returncode not in {0, 1}:
        raise SetforgeError("cannot classify project visibility index state")
    remove_from_index = (
        requested is ProjectVisibility.HIDDEN and tracked.returncode == 0
    )
    return (
        visibility_plan,
        _index_path(root) if remove_from_index else None,
        remove_from_index,
    )


def _find_record(
    root: Path, destination: Path
) -> tuple[Path, dict[str, object], bytes, dict[str, object]]:
    matches: list[tuple[Path, dict[str, object], bytes, dict[str, object]]] = []
    for record in _records():
        raw, payload = _load_manifest_payload(record)
        if raw["target"] != str(root):
            continue
        entries = raw["files"]
        assert isinstance(entries, list)
        for entry in entries:
            if (
                isinstance(entry, dict)
                and entry.get("destination") == destination.as_posix()
            ):
                matches.append((record, raw, payload, entry))
    if not matches:
        raise SetforgeError(
            f"project file is not recorded at this target: {destination}"
        )
    if len(matches) != 1:
        raise SetforgeError(
            f"project file has ambiguous recorded ownership: {destination}"
        )
    return matches[0]


def plan_project_visibility(
    target: Path, destination: Path, requested: ProjectVisibility
) -> ProjectVisibilityPlan:
    """Plan one uniquely recorded visibility transition without mutation."""
    root, actual_git_dir, _target_stat = _verified_project_target(target)
    if (
        destination.is_absolute()
        or destination == Path()
        or ".." in destination.parts
        or destination.as_posix() != str(destination)
    ):
        raise SetforgeError("project visibility file must be target-relative")
    record, raw, payload, entry = _find_record(root, destination)
    target_info = root.stat()
    if (
        raw["target_device"] != target_info.st_dev
        or raw["target_inode"] != target_info.st_ino
    ):
        raise SetforgeError("project target identity does not match the record")
    profile = str(raw["profile"])
    config_root = Path(str(raw["config_root"])).resolve(strict=True)
    plan_removal(profile=profile, target=root, config_root=config_root)
    git_dir = Path(str(raw["git_dir"])) if raw["git_dir"] is not None else None
    if git_dir != actual_git_dir:
        raise SetforgeError("project injection Git identity does not match the target")
    current = _actual_visibility(
        raw=raw,
        entry=entry,
        target=root,
        profile=profile,
        git_dir=git_dir,
        validate_declared=_manifest_schema(raw) == _MANIFEST_SCHEMA,
    )
    after = _render_manifest(raw, destination=destination, requested=requested)
    if git_dir is None:
        return ProjectVisibilityPlan(
            record,
            payload,
            payload,
            root,
            profile,
            destination,
            current,
            requested,
            config_root,
            None,
            None,
            None,
            None,
            False,
        )
    _require_compatible_visibility(
        target=root,
        manifest=record,
        visibility=requested,
        relative_paths={destination.as_posix()},
        ignored_claim_ids={
            claim_id(
                target_git_dir=git_dir,
                profile=profile,
                relative_path=destination.as_posix(),
            )
        },
    )
    visibility_plan: VisibilityPlan | None = None
    overlay_git_plan: OverlayGitPlan | None = None
    remove_from_index = False
    index_path: Path | None = None
    if (
        _manifest_schema(raw) < _MANIFEST_SCHEMA
        or _entry_action(entry) is ProjectFileAction.OVERLAY
    ):
        overlay_git_plan = _plan_overlay_visibility(
            root=root,
            git_dir=git_dir,
            profile=profile,
            raw=raw,
            selected=entry,
            destination=destination,
            current=current,
            requested=requested,
        )
    if _entry_action(entry) is not ProjectFileAction.OVERLAY:
        visibility_plan, index_path, remove_from_index = _plan_ordinary_visibility(
            root=root,
            git_dir=git_dir,
            profile=profile,
            destination=destination,
            requested=requested,
        )
    return ProjectVisibilityPlan(
        record,
        payload,
        after,
        root,
        profile,
        destination,
        current,
        requested,
        config_root,
        git_dir,
        visibility_plan,
        overlay_git_plan,
        index_path,
        remove_from_index,
    )


def apply_project_visibility(plan: ProjectVisibilityPlan) -> bool:
    """Apply one confirmed visibility plan as an exact journaled transaction."""
    if plan.git_dir is None or not plan.changed:
        return False
    operation_profile = (
        "project-visibility-"
        + _sha256(f"{plan.target}\0{plan.destination}".encode())[:24]
    )
    with mutation_locks(
        resources=True,
        config_dir=plan.config_root,
        target_roots=(plan.target,),
        profile=operation_profile,
    ) as guards:
        fresh = plan_project_visibility(plan.target, plan.destination, plan.requested)
        if fresh != plan:
            raise SetforgeError("project visibility plan changed before apply; retry")
        paths = tuple(
            dict.fromkeys(
                (
                    plan.manifest_path,
                    *((plan.index_path,) if plan.index_path is not None else ()),
                    *(
                        (plan.visibility_plan.exclude_path,)
                        if plan.visibility_plan is not None
                        else ()
                    ),
                    *(
                        (
                            plan.overlay_git_plan.config_path,
                            plan.overlay_git_plan.attributes_path,
                        )
                        if plan.overlay_git_plan is not None
                        else ()
                    ),
                )
            )
        )
        journal = operations.prepare(
            command="project-visibility",
            profile=operation_profile,
            config_dir=plan.config_root,
            resources_lock=True,
            command_line=(
                "project",
                "visibility",
                str(plan.target),
                str(plan.destination),
            ),
            paths=paths,
            path_guards=capture_parent_path_guards(paths),
        )
        with operations.recover_on_error(operation_profile, "project-visibility"):
            journal = operations.begin_checkpoint(
                journal,
                name="update-project-file-visibility",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery=(
                    "restore the exact project manifest and Git private/index state"
                ),
                paths=paths,
                restore_state=False,
                restore_transitions=False,
            )
            guards.verify_targets()
            if plan.remove_from_index:
                _run_git(
                    plan.target,
                    ["rm", "--cached", "-f", "--", plan.destination.as_posix()],
                )
            if plan.visibility_plan is not None:
                apply_claims(plan.visibility_plan)
            if plan.overlay_git_plan is not None:
                apply_overlay_git(plan.overlay_git_plan)
            atomicio.atomic_write_bytes(
                plan.manifest_path, plan.manifest_after, mode=0o600
            )
            journal = operations.finish_checkpoint(journal)
            operations.complete(journal)
    return True
