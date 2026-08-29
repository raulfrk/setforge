"""Reconcile recorded project injections with their current profiles."""

from __future__ import annotations

import base64
import binascii
import json
import stat
import uuid
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path

from setforge import atomicio, operations
from setforge.config import ProjectVisibility, load_config, resolve_project_profile
from setforge.errors import SetforgeError, StructuredParseError
from setforge.git_overlay import (
    OverlayClaim,
    apply_overlay_git,
    overlay_claim_id,
    plan_overlay_git,
)
from setforge.git_visibility import VisibilityClaim, apply_claims, claim_id, plan_claims
from setforge.locking import mutation_locks
from setforge.orphan_scan import capture_parent_path_guards
from setforge.ownership import (
    ClaimLifecycle,
    OwnershipStore,
    ProvenanceFact,
    ProvenanceFactKind,
    read_owner_id_locked,
    resolve_owner_common_dir,
)
from setforge.project_injection import (
    _MANIFEST_SCHEMA,
    _PRIOR_MANIFEST_SCHEMA,
    ProjectFileAction,
    ProjectFilePlan,
    _claim_fingerprint,
    _claim_matches_plan,
    _is_tracked,
    _load_manifest,
    _load_manifest_payload,
    _plan_file,
    _remove_created_parent,
    _require_guards,
    _resource_id,
    _sha256,
    _unlink_project_file,
    _verified_project_target,
    _write_project_file,
)
from setforge.project_overlay import (
    build_overlay,
    clean_content,
    overlay_path,
    read_overlay,
    update_local_content,
    write_overlay,
)
from setforge.reconcile.merge import merge as line_merge
from setforge.reconcile.merge import split_lines
from setforge.reconcile.merge_model import (
    Clean,
    Conflict,
    MergeInput,
    MergeResult,
    Segment,
)
from setforge.reconcile.structured_units import (
    _dump_model,
    _load_model,
    structured_format,
)
from setforge.reconcile.types import ABSENT
from setforge.reconcile.types import file_id as reconcile_file_id
from setforge.structural_merge import merge_structural
from setforge.transitions import state_root
from setforge.ui.primitives import CANCEL


@dataclass(frozen=True, slots=True)
class RecordedProjectInjection:
    """One strict private injection record bound to its source configuration."""

    profile: str
    target: Path
    git_dir: Path | None
    config_root: Path
    config_path: Path
    manifest_path: Path
    manifest_payload: bytes
    schema: int


@dataclass(frozen=True, slots=True)
class StoredProjectFile:
    """Strict file state decoded from one injection manifest."""

    file_id: str
    declaring_profile: str
    source: Path
    destination: Path
    action: ProjectFileAction
    applied_payload: bytes | None
    applied_digest: str | None
    applied_mode: int | None
    upstream_payload: bytes | None
    upstream_mode: int | None
    previous_payload: bytes | None
    previous_mode: int | None
    created_parents: tuple[Path, ...]
    visibility: ProjectVisibility


class SyncFileKind(StrEnum):
    """Membership effect calculated for one project file."""

    UPDATE = "update"
    ADD = "add"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class ProjectSyncFilePlan:
    """One no-write content decision in a target-wide sync plan."""

    profile: str
    kind: SyncFileKind
    file_id: str
    declaring_profile: str
    relative_destination: Path
    live: MergeInput
    live_mode: int | None
    desired_upstream: MergeInput
    desired_mode: int | None
    result_mode: int | None
    mode_conflict: bool
    result: MergeResult
    legacy: bool
    stored: StoredProjectFile | None = None
    addition: ProjectFilePlan | None = None
    overlay_base: bytes | None = None


@dataclass(frozen=True, slots=True)
class ProjectSyncPlan:
    """Every profile/file decision for one exact target, with no writes made."""

    target: Path
    injections: tuple[RecordedProjectInjection, ...]
    files: tuple[ProjectSyncFilePlan, ...]

    @property
    def conflicts(self) -> int:
        """Return the number of unresolved regions across the target batch."""
        content_conflicts = sum(
            isinstance(segment, Conflict)
            for item in self.files
            for segment in item.result.segments
        )
        return content_conflicts + sum(item.mode_conflict for item in self.files)


def discover_injections(target: Path) -> tuple[RecordedProjectInjection, ...]:
    """Discover every injection for exactly one verified Git worktree."""
    root, git_dir, target_stat = _verified_project_target(target)
    records_dir = state_root() / "project-injections"
    if not records_dir.exists():
        return ()
    records: list[RecordedProjectInjection] = []
    profiles: set[str] = set()
    for path in sorted(records_dir.glob("*.json")):
        raw, manifest_payload = _load_manifest_payload(path)
        if raw["target"] != str(root):
            continue
        if (
            raw["target_device"] != target_stat.st_dev
            or raw["target_inode"] != target_stat.st_ino
            or raw["git_dir"] != (str(git_dir) if git_dir is not None else None)
        ):
            raise SetforgeError(
                f"project injection state does not match target identity: {path}"
            )
        profile = raw["profile"]
        if not isinstance(profile, str) or not profile or profile in profiles:
            raise SetforgeError(
                f"project injection state has duplicate profile: {path}"
            )
        profiles.add(profile)
        config_root_raw = raw["config_root"]
        config_path_raw = raw.get("config_path")
        if not isinstance(config_root_raw, str) or (
            raw["schema"] in {_PRIOR_MANIFEST_SCHEMA, _MANIFEST_SCHEMA}
            and not isinstance(config_path_raw, str)
        ):
            raise SetforgeError(f"project injection config paths are invalid: {path}")
        try:
            config_root = Path(config_root_raw).resolve(strict=True)
            if raw["schema"] in {_PRIOR_MANIFEST_SCHEMA, _MANIFEST_SCHEMA}:
                assert isinstance(config_path_raw, str)
                config_path = Path(config_path_raw).resolve(strict=True)
            else:
                config_path = (config_root / "setforge.yaml").resolve(strict=True)
            config_path.relative_to(config_root)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise SetforgeError(
                f"project injection config cannot be resolved safely: {path}"
            ) from exc
        if not config_path.is_file():
            raise SetforgeError(
                f"project injection config is not a regular file: {config_path}"
            )
        schema = raw["schema"]
        assert isinstance(schema, int)
        assert not isinstance(schema, bool)
        records.append(
            RecordedProjectInjection(
                profile=profile,
                target=root,
                git_dir=git_dir,
                config_root=config_root,
                config_path=config_path,
                manifest_path=path,
                manifest_payload=manifest_payload,
                schema=schema,
            )
        )
    return tuple(sorted(records, key=lambda record: record.profile))


def _decode_payload(value: object, *, field: str) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SetforgeError(f"project injection state has invalid {field}")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SetforgeError(f"project injection state has invalid {field}") from exc


def _stored_files(  # noqa: C901 - one fail-closed parser for untrusted state
    record: RecordedProjectInjection,
) -> tuple[StoredProjectFile, ...]:
    raw = _load_manifest(record.manifest_path)
    raw_files = raw["files"]
    assert isinstance(raw_files, list)
    files: list[StoredProjectFile] = []
    destinations: set[Path] = set()
    file_ids: set[str] = set()
    legacy_fields = {
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
    current_fields = legacy_fields | {
        "applied_payload",
        "upstream_mode",
        "upstream_payload",
    }
    newest_fields = current_fields | {"visibility"}
    for entry in raw_files:
        if not isinstance(entry, dict) or set(entry) != (
            newest_fields
            if record.schema == _MANIFEST_SCHEMA
            else current_fields
            if record.schema == _PRIOR_MANIFEST_SCHEMA
            else legacy_fields
        ):
            raise SetforgeError("project injection state has invalid file fields")
        try:
            relative = Path(entry["destination"])
            file_id = entry["file_id"]
            declaring_profile = entry["declaring_profile"]
            source = Path(entry["source"])
            action = ProjectFileAction(entry["action"])
            applied_digest = entry["applied_digest"]
            source_digest = entry["source_digest"]
            visibility = ProjectVisibility(
                str(
                    entry["visibility"]
                    if record.schema == _MANIFEST_SCHEMA
                    else raw["visibility"]
                )
            )
            applied_mode = entry["applied_mode"]
            previous_mode = entry["previous_mode"]
            created_parents_raw = entry["created_parents"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SetforgeError(
                "project injection state has an invalid file record"
            ) from exc
        if (
            not isinstance(file_id, str)
            or not file_id
            or not isinstance(declaring_profile, str)
            or not declaring_profile
            or not isinstance(source_digest, str)
            or not isinstance(created_parents_raw, list)
            or relative.is_absolute()
            or relative == Path()
            or ".." in relative.parts
            or relative in destinations
            or file_id in file_ids
        ):
            raise SetforgeError("project injection state has an invalid file record")
        if record.schema == 1 and (
            not isinstance(applied_digest, str) or not _valid_mode(applied_mode)
        ):
            raise SetforgeError("project injection state has an invalid file record")
        if previous_mode is not None and not _valid_mode(previous_mode):
            raise SetforgeError("project injection state has an invalid file record")
        previous_payload = _decode_payload(
            entry["previous_payload"], field="previous payload"
        )
        applied_payload = (
            _decode_payload(entry["applied_payload"], field="applied payload")
            if record.schema in {_PRIOR_MANIFEST_SCHEMA, _MANIFEST_SCHEMA}
            else None
        )
        upstream_payload = (
            _decode_payload(entry["upstream_payload"], field="upstream payload")
            if record.schema in {_PRIOR_MANIFEST_SCHEMA, _MANIFEST_SCHEMA}
            else None
        )
        upstream_mode_raw = entry.get("upstream_mode")
        upstream_mode = upstream_mode_raw if _valid_mode(upstream_mode_raw) else None
        applied_absent = (
            applied_payload is None and applied_digest is None and applied_mode is None
        )
        applied_present = (
            applied_payload is not None
            and isinstance(applied_digest, str)
            and _valid_mode(applied_mode)
        )
        if record.schema in {_PRIOR_MANIFEST_SCHEMA, _MANIFEST_SCHEMA} and (
            (not applied_absent and not applied_present)
            or upstream_payload is None
            or upstream_mode is None
            or isinstance(upstream_mode, bool)
            or (
                applied_payload is not None
                and _sha256(applied_payload) != applied_digest
            )
            or _sha256(upstream_payload) != source_digest
        ):
            raise SetforgeError(
                "project injection state has an inconsistent file record"
            )
        baseline_absent = previous_payload is None and previous_mode is None
        baseline_present = previous_payload is not None and previous_mode is not None
        if (
            (action is ProjectFileAction.CREATE and not baseline_absent)
            or (action is not ProjectFileAction.CREATE and not baseline_present)
            or (record.schema == 1 and applied_digest != source_digest)
        ):
            raise SetforgeError(
                "project injection state has an inconsistent file record"
            )
        parents: list[Path] = []
        for value in created_parents_raw:
            if not isinstance(value, str):
                raise SetforgeError(
                    "project injection state has an invalid parent record"
                )
            parent_relative = Path(value)
            parent = record.target / parent_relative
            destination = record.target / relative
            if (
                parent_relative.is_absolute()
                or ".." in parent_relative.parts
                or parent == record.target
                or parent not in destination.parents
                or parent in parents
            ):
                raise SetforgeError(
                    "project injection state has an invalid parent record"
                )
            parents.append(parent)
        destinations.add(relative)
        file_ids.add(file_id)
        files.append(
            StoredProjectFile(
                file_id=file_id,
                declaring_profile=declaring_profile,
                source=source,
                destination=relative,
                action=action,
                applied_payload=applied_payload,
                applied_digest=applied_digest,
                applied_mode=applied_mode,
                upstream_payload=upstream_payload,
                upstream_mode=upstream_mode,
                previous_payload=previous_payload,
                previous_mode=previous_mode,
                created_parents=tuple(parents),
                visibility=visibility,
            )
        )
    return tuple(files)


def _read_live(
    target: Path, relative: Path, *, allow_tracked: bool, git: bool
) -> tuple[MergeInput, int | None]:
    destination = target / relative
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return ABSENT, None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SetforgeError(
            f"injected project file is not an ordinary regular file: {destination}"
        )
    if git and _is_tracked(target, relative) and not allow_tracked:
        raise SetforgeError(
            f"project destination unexpectedly became tracked by Git: {relative}"
        )
    return destination.read_bytes(), stat.S_IMODE(info.st_mode)


def _valid_mode(value: object) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0o7777
    )


def _clean_result(value: MergeInput) -> MergeResult:
    return (
        MergeResult((), absent=True)
        if value is ABSENT
        else MergeResult((Clean(value),))
    )


def _legacy_result(
    *,
    live: MergeInput,
    live_mode: int | None,
    stored: StoredProjectFile,
    desired: MergeInput,
) -> MergeResult:
    if live is ABSENT or desired is ABSENT:
        return MergeResult(
            (
                Conflict(
                    b"",
                    b"" if live is ABSENT else live,
                    b"" if desired is ABSENT else desired,
                ),
            )
        )
    return legacy_two_way_merge(live, desired)


def _merge_mode(
    base: int | None, ours: int | None, theirs: int | None
) -> tuple[int | None, bool]:
    if ours == theirs:
        return ours, False
    if ours == base:
        return theirs, False
    if theirs == base:
        return ours, False
    return ours, True


def _merge_overlay_update(
    relative: Path, old_profile: bytes, live: bytes, desired: bytes
) -> MergeResult:
    """Apply exact profile deltas, falling back to the normal conflict model."""
    try:
        return _clean_result(update_local_content(old_profile, desired, live))
    except SetforgeError:
        return merge_project_content(relative, old_profile, live, desired)


def plan_sync(target: Path) -> ProjectSyncPlan:
    """Build an immutable target-wide sync plan without changing any state."""
    injections = discover_injections(target)
    if not injections:
        raise SetforgeError(f"no project injections are recorded for: {target}")
    planned: list[ProjectSyncFilePlan] = []
    claimed_destinations: dict[Path, str] = {}
    for injection in injections:
        config = load_config(injection.config_path)
        resolved = resolve_project_profile(
            config, injection.profile, injection.config_root
        )
        stored_by_destination = {
            item.destination: item for item in _stored_files(injection)
        }
        current_by_destination = {item.dst: item for item in resolved.files}
        for relative in sorted(
            stored_by_destination.keys() | current_by_destination.keys(), key=str
        ):
            owner = claimed_destinations.get(relative)
            if owner is not None:
                raise SetforgeError(
                    f"project profiles {owner!r} and {injection.profile!r} both "
                    f"claim {relative}"
                )
            claimed_destinations[relative] = injection.profile
            stored = stored_by_destination.get(relative)
            current = current_by_destination.get(relative)
            if stored is None:
                assert current is not None
                addition = _plan_file(
                    injection.target, current, git=injection.git_dir is not None
                )
                live: MergeInput = (
                    ABSENT
                    if addition.previous_payload is None
                    else addition.previous_payload
                )
                result = (
                    _clean_result(addition.source_payload)
                    if live is ABSENT
                    else legacy_two_way_merge(live, addition.source_payload)
                )
                result_mode, mode_conflict = _merge_mode(
                    None,
                    addition.previous_mode,
                    addition.source_mode,
                )
                planned.append(
                    ProjectSyncFilePlan(
                        profile=injection.profile,
                        kind=SyncFileKind.ADD,
                        file_id=current.id,
                        declaring_profile=current.declaring_profile,
                        relative_destination=relative,
                        live=live,
                        live_mode=addition.previous_mode,
                        desired_upstream=addition.source_payload,
                        desired_mode=addition.source_mode,
                        result_mode=result_mode,
                        mode_conflict=mode_conflict,
                        result=result,
                        legacy=False,
                        addition=addition,
                        overlay_base=(
                            addition.previous_payload
                            if addition.action is ProjectFileAction.OVERLAY
                            else None
                        ),
                    )
                )
                continue
            live, live_mode = _read_live(
                injection.target,
                relative,
                allow_tracked=stored.action is ProjectFileAction.OVERLAY,
                git=injection.git_dir is not None,
            )
            overlay_base: bytes | None = None
            if stored.action is ProjectFileAction.OVERLAY:
                if not isinstance(live, bytes):
                    raise SetforgeError(
                        f"tracked project overlay is absent: {relative}"
                    )
                overlay = read_overlay(injection.target, relative)
                if overlay is None or overlay.local != stored.applied_payload:
                    raise SetforgeError(
                        f"tracked project overlay is missing or mismatched: {relative}"
                    )
                overlay_base = clean_content(overlay, live)
            if current is None:
                desired: MergeInput = (
                    overlay_base
                    if overlay_base is not None
                    else ABSENT
                    if stored.previous_payload is None
                    else stored.previous_payload
                )
                result = (
                    _clean_result(overlay_base)
                    if overlay_base is not None
                    else merge_project_content(
                        relative, stored.upstream_payload, live, desired
                    )
                    if stored.upstream_payload is not None
                    else _legacy_result(
                        live=live,
                        live_mode=live_mode,
                        stored=stored,
                        desired=desired,
                    )
                )
                result_mode, mode_conflict = _merge_mode(
                    (
                        stored.upstream_mode
                        if stored.upstream_mode is not None
                        else stored.applied_mode
                    ),
                    live_mode,
                    stored.previous_mode,
                )
                planned.append(
                    ProjectSyncFilePlan(
                        profile=injection.profile,
                        kind=SyncFileKind.REMOVE,
                        file_id=stored.file_id,
                        declaring_profile=stored.declaring_profile,
                        relative_destination=relative,
                        live=live,
                        live_mode=live_mode,
                        desired_upstream=desired,
                        desired_mode=stored.previous_mode,
                        result_mode=result_mode,
                        mode_conflict=mode_conflict,
                        result=result,
                        legacy=injection.schema == 1,
                        stored=stored,
                    )
                )
                continue
            addition = _plan_file(
                injection.target, current, git=injection.git_dir is not None
            )
            desired = addition.source_payload
            result = (
                _merge_overlay_update(
                    relative,
                    stored.upstream_payload,
                    live,
                    desired,
                )
                if (
                    stored.action is ProjectFileAction.OVERLAY
                    and stored.upstream_payload is not None
                    and isinstance(live, bytes)
                )
                else merge_project_content(
                    relative, stored.upstream_payload, live, desired
                )
                if stored.upstream_payload is not None
                else _legacy_result(
                    live=live,
                    live_mode=live_mode,
                    stored=stored,
                    desired=desired,
                )
            )
            result_mode, mode_conflict = _merge_mode(
                (
                    stored.upstream_mode
                    if stored.upstream_mode is not None
                    else stored.applied_mode
                ),
                live_mode,
                addition.source_mode,
            )
            planned.append(
                ProjectSyncFilePlan(
                    profile=injection.profile,
                    kind=SyncFileKind.UPDATE,
                    file_id=current.id,
                    declaring_profile=current.declaring_profile,
                    relative_destination=relative,
                    live=live,
                    live_mode=live_mode,
                    desired_upstream=desired,
                    desired_mode=addition.source_mode,
                    result_mode=result_mode,
                    mode_conflict=mode_conflict,
                    result=result,
                    legacy=injection.schema == 1,
                    stored=stored,
                    addition=addition,
                    overlay_base=overlay_base,
                )
            )
    return ProjectSyncPlan(
        target=injections[0].target,
        injections=injections,
        files=tuple(planned),
    )


class AutoResolution(StrEnum):
    """Explicit policy for resolving every project-sync conflict."""

    KEEP_LIVE = "keep-live"
    USE_PROFILE = "use-profile"


def merge_project_content(
    path: Path, base: MergeInput, ours: MergeInput, theirs: MergeInput
) -> MergeResult:
    """Three-way project content, preferring key-aware clean structured merges."""
    fmt = structured_format(path)
    if (
        fmt is not None
        and isinstance(base, bytes)
        and isinstance(ours, bytes)
        and isinstance(theirs, bytes)
    ):
        try:
            structured = merge_structural(
                _load_model(base, fmt),
                _load_model(ours, fmt),
                _load_model(theirs, fmt),
            )
            if structured.clean:
                return MergeResult((Clean(_dump_model(structured.merged_model, fmt)),))
        except (StructuredParseError, TypeError, ValueError):
            pass
    return line_merge(base, ours, theirs)


def legacy_two_way_merge(ours: bytes, theirs: bytes) -> MergeResult:
    """Represent a no-ancestor comparison as independently resolvable hunks.

    With no common ancestor, SetForge cannot safely attribute a difference to
    either side. Equal ranges are retained and every differing opcode is exposed
    as a conflict compatible with the existing reconciliation wizard.
    """
    ours_lines = split_lines(ours)
    theirs_lines = split_lines(theirs)
    segments: list[Segment] = []
    matcher = SequenceMatcher(None, ours_lines, theirs_lines, autojunk=False)
    for tag, ours_start, ours_end, theirs_start, theirs_end in matcher.get_opcodes():
        ours_region = b"".join(ours_lines[ours_start:ours_end])
        theirs_region = b"".join(theirs_lines[theirs_start:theirs_end])
        if tag == "equal":
            segments.append(Clean(ours_region))
        else:
            segments.append(Conflict(b"", ours_region, theirs_region))
    return MergeResult(tuple(segments))


def resolve_automatically(result: MergeResult, policy: AutoResolution) -> MergeResult:
    """Resolve all conflict regions with one explicit non-interactive policy."""
    resolved: list[Clean] = []
    for segment in result.segments:
        if isinstance(segment, Clean):
            resolved.append(segment)
        elif policy is AutoResolution.KEEP_LIVE:
            resolved.append(Clean(segment.ours))
        else:
            resolved.append(Clean(segment.theirs))
    return MergeResult(tuple(resolved), absent=result.absent)


def resolve_sync_plan(
    plan: ProjectSyncPlan,
    *,
    auto: AutoResolution | None = None,
    interactive: bool = False,
) -> ProjectSyncPlan | None:
    """Resolve every conflict, returning ``None`` when the batch is deferred."""
    resolved_files: list[ProjectSyncFilePlan] = []
    for item in plan.files:
        interactive_chose_absent = False
        if item.result.clean and not item.mode_conflict:
            resolved_files.append(item)
            continue
        if not item.result.clean and auto is not None:
            result = resolve_automatically(item.result, auto)
        elif not item.result.clean and interactive:
            from setforge.reconcile.wizard import resolve_conflicts

            wizard = resolve_conflicts(
                reconcile_file_id(f"{item.profile}/{item.file_id}"),
                item.result,
                display_path=item.relative_destination.as_posix(),
            )
            if wizard is CANCEL or wizard.deferred:
                return None
            result = wizard.merged
            selection = wizard.selections[0] if len(wizard.selections) == 1 else None
            interactive_chose_absent = (
                item.live is ABSENT and selection == "ours"
            ) or (item.desired_upstream is ABSENT and selection == "theirs")
        elif not item.result.clean:
            raise SetforgeError(
                f"project sync has unresolved conflicts in "
                f"{item.relative_destination}; use a TTY or --auto"
            )
        else:
            result = item.result
        merged = result.merged()
        chose_absent = merged == b"" and (
            interactive_chose_absent
            or (item.live is ABSENT and auto is AutoResolution.KEEP_LIVE)
            or (item.desired_upstream is ABSENT and auto is AutoResolution.USE_PROFILE)
        )
        if chose_absent:
            result = MergeResult((), absent=True)
        if item.mode_conflict:
            if auto is None:
                raise SetforgeError(
                    f"project sync has an unresolved mode conflict in "
                    f"{item.relative_destination}; use --auto"
                )
            result_mode = (
                item.live_mode
                if auto is AutoResolution.KEEP_LIVE
                else item.desired_mode
            )
        else:
            result_mode = item.result_mode
        resolved_files.append(
            replace(
                item,
                result=result,
                result_mode=result_mode,
                mode_conflict=False,
            )
        )
    return replace(plan, files=tuple(resolved_files))


def _encode_payload(value: bytes | None) -> str | None:
    return base64.b64encode(value).decode("ascii") if value is not None else None


def render_sync_manifests(plan: ProjectSyncPlan) -> dict[Path, bytes]:
    """Render strict schema-2 manifests for a fully resolved sync plan."""
    if plan.conflicts:
        raise SetforgeError("cannot render unresolved project sync manifests")
    rendered: dict[Path, bytes] = {}
    for injection in plan.injections:
        raw = json.loads(injection.manifest_payload)
        assert isinstance(raw, dict)
        entries: list[dict[str, object]] = []
        for item in (row for row in plan.files if row.profile == injection.profile):
            if item.kind is SyncFileKind.REMOVE:
                continue
            if item.desired_upstream is ABSENT or item.desired_mode is None:
                raise SetforgeError("current project member has no upstream payload")
            applied = item.result.merged()
            applied_payload = None if applied is ABSENT else applied
            stored = item.stored
            addition = item.addition
            if addition is None:
                raise SetforgeError("current project member lost its source plan")
            previous_payload = (
                stored.previous_payload
                if stored is not None
                else addition.previous_payload
            )
            previous_mode = (
                stored.previous_mode if stored is not None else addition.previous_mode
            )
            action = stored.action if stored is not None else addition.action
            if action is ProjectFileAction.OVERLAY:
                if item.overlay_base is None:
                    raise SetforgeError(
                        "tracked project sync has no Git-facing overlay base"
                    )
                previous_payload = item.overlay_base
                previous_mode = item.live_mode
            created_parents = (
                stored.created_parents
                if stored is not None
                else addition.created_parents
            )
            visibility = (
                stored.visibility
                if stored is not None
                else ProjectVisibility(str(raw["visibility"]))
            )
            entries.append(
                {
                    "action": action.value,
                    "applied_digest": (
                        _sha256(applied_payload)
                        if applied_payload is not None
                        else None
                    ),
                    "applied_mode": item.result_mode,
                    "applied_payload": _encode_payload(applied_payload),
                    "created_parents": [
                        parent.relative_to(plan.target).as_posix()
                        for parent in created_parents
                    ],
                    "declaring_profile": item.declaring_profile,
                    "destination": item.relative_destination.as_posix(),
                    "file_id": item.file_id,
                    "previous_mode": previous_mode,
                    "previous_payload": _encode_payload(previous_payload),
                    "source": str(addition.source),
                    "source_digest": _sha256(item.desired_upstream),
                    "upstream_mode": item.desired_mode,
                    "upstream_payload": _encode_payload(item.desired_upstream),
                    "visibility": visibility.value,
                }
            )
        raw["schema"] = _MANIFEST_SCHEMA
        raw["config_path"] = str(injection.config_path)
        raw["files"] = entries
        rendered[injection.manifest_path] = (
            json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
    return rendered


def _basis(item: ProjectSyncFilePlan) -> tuple[object, ...]:
    return (
        item.profile,
        item.kind,
        item.file_id,
        item.declaring_profile,
        item.relative_destination,
        item.live,
        item.live_mode,
        item.desired_upstream,
        item.desired_mode,
        item.stored,
        item.addition,
        item.legacy,
        item.overlay_base,
    )


def _ownership_plan(item: ProjectSyncFilePlan, target: Path) -> ProjectFilePlan:
    stored = item.stored
    addition = item.addition
    mode: int | None
    if item.desired_upstream is ABSENT or item.desired_mode is None:
        if stored is None or stored.upstream_payload is None:
            raise SetforgeError("project ownership state has no upstream payload")
        payload = stored.upstream_payload
        mode = stored.upstream_mode
        source = stored.source
        action = stored.action
        previous_payload = stored.previous_payload
        previous_mode = stored.previous_mode
        parents = stored.created_parents
    else:
        if stored is None and addition is None:
            raise SetforgeError("project member has no ownership source plan")
        payload = item.desired_upstream
        mode = item.desired_mode
        if stored is not None:
            source = addition.source if addition is not None else stored.source
            action = stored.action
            previous_payload = stored.previous_payload
            previous_mode = stored.previous_mode
            parents = stored.created_parents
        else:
            assert addition is not None
            source = addition.source
            action = addition.action
            previous_payload = addition.previous_payload
            previous_mode = addition.previous_mode
            parents = addition.created_parents
    if mode is None:
        raise SetforgeError("project ownership state has no upstream mode")
    merged = item.result.merged() if item.result.clean else None
    applied_payload = merged if isinstance(merged, bytes) else None
    return ProjectFilePlan(
        file_id=item.file_id,
        declaring_profile=item.declaring_profile,
        source=source,
        destination=target / item.relative_destination,
        relative_destination=item.relative_destination,
        source_payload=payload,
        source_mode=mode,
        source_digest=_sha256(payload),
        applied_payload=applied_payload,
        action=action,
        previous_payload=previous_payload,
        previous_mode=previous_mode,
        created_parents=parents,
    )


def _prior_ownership_plan(item: ProjectSyncFilePlan, target: Path) -> ProjectFilePlan:
    stored = item.stored
    if stored is None:
        raise SetforgeError("project member has no prior ownership state")
    payload = stored.upstream_payload
    mode: int | None
    if payload is None:
        if stored.applied_digest is None or stored.applied_mode is None:
            raise SetforgeError("legacy project ownership state is incomplete")
        payload = item.live if isinstance(item.live, bytes) else b""
        digest = stored.applied_digest
        mode = stored.applied_mode
    else:
        digest = _sha256(payload)
        mode = stored.upstream_mode
    if mode is None:
        raise SetforgeError("project ownership state has no prior mode")
    return ProjectFilePlan(
        file_id=stored.file_id,
        declaring_profile=stored.declaring_profile,
        source=stored.source,
        destination=target / stored.destination,
        relative_destination=stored.destination,
        source_payload=payload,
        source_mode=mode,
        source_digest=digest,
        applied_payload=(item.live if isinstance(item.live, bytes) else None),
        action=stored.action,
        previous_payload=stored.previous_payload,
        previous_mode=stored.previous_mode,
        created_parents=stored.created_parents,
    )


def apply_sync(plan: ProjectSyncPlan) -> bool:  # noqa: C901
    """Apply one fully resolved target-wide plan as a journaled transaction."""
    if plan.conflicts:
        raise SetforgeError("cannot apply an unresolved project sync plan")
    operation_profile = "project-sync-" + _sha256(str(plan.target).encode())[:24]
    config_roots = tuple(
        sorted({item.config_root for item in plan.injections}, key=str)
    )
    identity_dirs = tuple(
        sorted({resolve_owner_common_dir(path) for path in config_roots}, key=str)
    )
    with mutation_locks(
        resources=True,
        config_identity_dirs=identity_dirs,
        config_dirs=config_roots,
        target_roots=(plan.target,),
        profile=operation_profile,
    ) as guards:
        _require_guards(guards, plan.target)
        fresh = plan_sync(plan.target)
        if fresh.injections != plan.injections or tuple(
            map(_basis, fresh.files)
        ) != tuple(map(_basis, plan.files)):
            raise SetforgeError("project sync plan changed before apply; retry")
        identity_by_dir = {
            guard.common_dir: guard for guard in guards.config_identities
        }
        owners: dict[str, uuid.UUID] = {}
        raw_by_profile: dict[str, dict[str, object]] = {}
        for injection in plan.injections:
            raw, manifest_payload = _load_manifest_payload(injection.manifest_path)
            if manifest_payload != injection.manifest_payload:
                raise SetforgeError("project sync plan changed before apply; retry")
            raw_by_profile[injection.profile] = raw
            try:
                recorded_owner = uuid.UUID(str(raw["config_owner_id"]))
                identity_dir = resolve_owner_common_dir(injection.config_root)
                identity = identity_by_dir[identity_dir]
                actual_owner = read_owner_id_locked(
                    injection.config_root, identity.directory_fd
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SetforgeError(
                    "project injection has invalid config ownership state"
                ) from exc
            if actual_owner != recorded_owner:
                raise SetforgeError(
                    "project injection belongs to a different config checkout"
                )
            owners[injection.profile] = recorded_owner

        store = OwnershipStore()
        ownership_plans = {
            (item.profile, item.relative_destination): _ownership_plan(
                item, plan.target
            )
            for item in plan.files
        }
        resources = {key: _resource_id(plan.target, key[1]) for key in ownership_plans}
        prior_claims = {
            key: store.read(resource) for key, resource in resources.items()
        }
        for item in plan.files:
            key = (item.profile, item.relative_destination)
            claim = prior_claims[key]
            ownership_plan = ownership_plans[key]
            if item.kind is SyncFileKind.ADD:
                if claim is not None and not _claim_matches_plan(
                    claim,
                    resource=resources[key],
                    owner_id=owners[item.profile],
                    profile=item.profile,
                    item=ownership_plan,
                    lifecycle=ClaimLifecycle.RELEASED,
                ):
                    raise SetforgeError(
                        "a project destination already has an active ownership claim"
                    )
            elif claim is None or not _claim_matches_plan(
                claim,
                resource=resources[key],
                owner_id=owners[item.profile],
                profile=item.profile,
                item=_prior_ownership_plan(item, plan.target),
                lifecycle=ClaimLifecycle.CLAIMED,
            ):
                raise SetforgeError(
                    "project injection ownership state is missing or mismatched"
                )

        visibility_add: list[VisibilityClaim] = []
        visibility_remove: list[VisibilityClaim] = []
        overlay_add: list[OverlayClaim] = []
        overlay_remove: list[OverlayClaim] = []
        injection_by_profile = {
            injection.profile: injection for injection in plan.injections
        }
        for item in plan.files:
            raw = raw_by_profile[item.profile]
            injection = injection_by_profile[item.profile]
            git_dir = injection.git_dir
            action = (
                item.stored.action
                if item.stored is not None
                else item.addition.action
                if item.addition is not None
                else None
            )
            file_visibility = (
                item.stored.visibility
                if item.stored is not None
                else ProjectVisibility(str(raw["visibility"]))
            )
            if action is ProjectFileAction.OVERLAY and git_dir is not None:
                overlay_claim = OverlayClaim(
                    overlay_claim_id(
                        git_dir=git_dir,
                        profile=item.profile,
                        relative_path=item.relative_destination.as_posix(),
                    ),
                    item.relative_destination.as_posix(),
                )
                if item.kind is SyncFileKind.REMOVE and (
                    injection.schema < _MANIFEST_SCHEMA
                    or file_visibility is ProjectVisibility.HIDDEN
                ):
                    overlay_remove.append(overlay_claim)
                elif file_visibility is ProjectVisibility.HIDDEN:
                    overlay_add.append(overlay_claim)
                elif item.stored is not None and injection.schema < _MANIFEST_SCHEMA:
                    overlay_remove.append(overlay_claim)
                continue
            if file_visibility is not ProjectVisibility.HIDDEN:
                continue
            if git_dir is None:
                continue
            visibility_claim = VisibilityClaim(
                claim_id=claim_id(
                    target_git_dir=git_dir,
                    profile=item.profile,
                    relative_path=item.relative_destination.as_posix(),
                ),
                relative_path=item.relative_destination.as_posix(),
            )
            if item.kind is SyncFileKind.ADD:
                visibility_add.append(visibility_claim)
            elif item.kind is SyncFileKind.REMOVE:
                visibility_remove.append(visibility_claim)
        git_target = any(injection.git_dir is not None for injection in plan.injections)
        visibility_plan = (
            plan_claims(
                plan.target,
                add=tuple(visibility_add),
                remove=tuple(visibility_remove),
            )
            if git_target
            else None
        )
        overlay_git_plan = (
            plan_overlay_git(
                plan.target,
                add=tuple(overlay_add),
                remove=tuple(overlay_remove),
            )
            if git_target and (overlay_add or overlay_remove)
            else None
        )
        manifests = render_sync_manifests(plan)
        state_changed = any(
            not path.exists() or path.read_bytes() != payload
            for path, payload in manifests.items()
        )
        paths = tuple(
            dict.fromkeys(
                [
                    *(plan.target / item.relative_destination for item in plan.files),
                    *manifests,
                    *(store.claim_path(resource) for resource in resources.values()),
                    *(
                        (visibility_plan.exclude_path,)
                        if visibility_plan is not None
                        else ()
                    ),
                    *(
                        (
                            overlay_git_plan.config_path,
                            overlay_git_plan.attributes_path,
                        )
                        if overlay_git_plan is not None
                        else ()
                    ),
                    *(
                        overlay_path(plan.target, item.relative_destination)
                        for item in plan.files
                        if (
                            (
                                item.stored is not None
                                and item.stored.action is ProjectFileAction.OVERLAY
                            )
                            or (
                                item.addition is not None
                                and item.addition.action is ProjectFileAction.OVERLAY
                            )
                        )
                    ),
                    *(
                        parent
                        for item in plan.files
                        if item.kind is SyncFileKind.REMOVE and item.stored is not None
                        for parent in item.stored.created_parents
                    ),
                ]
            )
        )
        journal = operations.prepare(
            command="project-sync",
            profile=operation_profile,
            config_dir=None,
            config_dirs=config_roots,
            resources_lock=True,
            command_line=("project", "sync", str(plan.target)),
            paths=paths,
            path_guards=capture_parent_path_guards(paths),
        )
        changed = False
        with operations.recover_on_error(operation_profile, "project-sync"):
            journal = operations.begin_checkpoint(
                journal,
                name="synchronize-project-files-and-state",
                kind=operations.CheckpointKind.REVERSIBLE,
                recovery=(
                    "restore all project files, manifests, ownership, and visibility"
                ),
                paths=paths,
                restore_state=False,
                restore_transitions=False,
            )
            for item in plan.files:
                guards.verify_targets()
                merged = item.result.merged()
                if merged is ABSENT:
                    if item.live is not ABSENT:
                        _unlink_project_file(
                            guards.targets[0], item.relative_destination
                        )
                        changed = True
                elif merged != item.live or item.result_mode != item.live_mode:
                    if item.result_mode is None:
                        raise SetforgeError("project sync result has no file mode")
                    _write_project_file(
                        guards.targets[0],
                        item.relative_destination,
                        merged,
                        item.result_mode,
                    )
                    changed = True
                action = (
                    item.stored.action
                    if item.stored is not None
                    else item.addition.action
                    if item.addition is not None
                    else None
                )
                if (
                    action is ProjectFileAction.OVERLAY
                    and item.kind is not SyncFileKind.REMOVE
                ):
                    if item.overlay_base is None or not isinstance(merged, bytes):
                        raise SetforgeError(
                            "tracked project sync has incomplete overlay state"
                        )
                    write_overlay(
                        build_overlay(
                            plan.target,
                            item.relative_destination,
                            item.overlay_base,
                            merged,
                        )
                    )
                    changed = True
                key = (item.profile, item.relative_destination)
                resource = resources[key]
                claim = prior_claims[key]
                if item.kind is SyncFileKind.REMOVE:
                    assert claim is not None
                    store.release_locked(
                        resource,
                        expected_owner=owners[item.profile],
                        expected_generation=claim.generation,
                    )
                else:
                    expected_generation = (
                        claim.generation if claim is not None else None
                    )
                    if claim is not None and claim.lifecycle is ClaimLifecycle.RELEASED:
                        claim = store.restore_locked(claim)
                        expected_generation = claim.generation
                    ownership_plan = ownership_plans[key]
                    store.claim_locked(
                        resource_id=resource,
                        owner_id=owners[item.profile],
                        declaration_refs=(
                            f"project-profile:{item.profile}:{item.file_id}",
                        ),
                        provenance=(
                            ProvenanceFact(
                                ProvenanceFactKind.ORIGIN, "project-profile"
                            ),
                            ProvenanceFact(
                                ProvenanceFactKind.ARTIFACT,
                                ownership_plan.source_digest,
                            ),
                        ),
                        locator=str(ownership_plan.destination),
                        fingerprint=_claim_fingerprint(ownership_plan),
                        expected_generation=expected_generation,
                    )
            if visibility_plan is not None:
                apply_claims(visibility_plan)
            if overlay_git_plan is not None:
                apply_overlay_git(overlay_git_plan)
            for item in plan.files:
                if (
                    item.kind is SyncFileKind.REMOVE
                    and item.stored is not None
                    and item.stored.action is ProjectFileAction.OVERLAY
                ):
                    overlay_path(plan.target, item.relative_destination).unlink()
            for path, payload in manifests.items():
                atomicio.atomic_write_bytes(path, payload, mode=0o600)
            for item in plan.files:
                if item.kind is not SyncFileKind.REMOVE or item.stored is None:
                    continue
                for parent in sorted(
                    item.stored.created_parents,
                    key=lambda value: len(value.parts),
                    reverse=True,
                ):
                    _remove_created_parent(
                        guards.targets[0], parent.relative_to(plan.target)
                    )
            journal = operations.finish_checkpoint(journal)
            operations.complete(journal)
        return (
            changed
            or (visibility_plan is not None and visibility_plan.changed)
            or state_changed
        )
