"""Bounded discovery of unrecorded files in SetForge-managed trees."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import chain
from pathlib import Path

from jinja2 import TemplateError

from setforge import codex_lifecycle, operations, paths
from setforge import compare as compare_mod
from setforge.config import (
    Config,
    FileComponent,
    ResolvedProfile,
    resolve_effective_profile,
    resolve_profile,
)
from setforge.errors import ConfigError, SetforgeError
from setforge.paths import journals_root, snapshots_root, state_root


class ScanEntryKind(StrEnum):
    """Filesystem kinds the scan can surface."""

    REGULAR = "regular"
    SYMLINK = "symlink"
    UNSUPPORTED = "unsupported"


class _WalkSkip(StrEnum):
    MOUNT = "mount"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class PathIdentity:
    """Stable lstat identity for one directory entry."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> PathIdentity:
        return cls(
            device=info.st_dev,
            inode=info.st_ino,
            mode=info.st_mode,
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class ScanEntry:
    """One unrecorded leaf and the identities approved by the operator."""

    path: Path
    kind: ScanEntryKind
    identity: PathIdentity
    parent_identities: tuple[tuple[Path, PathIdentity], ...]
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class UnresolvedProfile:
    """A profile the scan could only partly resolve, and why."""

    profile: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Deterministically ordered scan candidates and skipped-type count."""

    entries: tuple[ScanEntry, ...]
    skipped_unsupported: int = 0
    skipped_mounts: int = 0
    unresolved: tuple[UnresolvedProfile, ...] = ()


@dataclass(frozen=True, slots=True)
class _ManagedInventory:
    roots: tuple[Path, ...]
    attributed: frozenset[Path]
    excluded_roots: tuple[Path, ...]
    unresolved: tuple[UnresolvedProfile, ...] = ()


def scan_unrecorded_managed_tree(
    config: Config,
    repo_root: Path,
    *,
    config_path: Path,
    transitions_dir: Path,
    profile: str | None = None,
) -> ScanResult:
    """Return unrecorded leaves below bounded, currently managed roots.

    Every configured profile is resolved independently so bundle expansion and
    host-local destination overrides contribute to the attribution set. The
    walker never follows symlinks and never crosses a filesystem boundary.
    A destination template that will not render is an error only in ``profile``
    (the selected one); in any other profile that file is left out. A bundle
    file component left out that way hides where the profile deployed it, so
    the profile is reported as unresolved and no directory only that profile
    uses is scanned.
    """
    inventory = _managed_inventory(
        config,
        repo_root,
        config_path=config_path,
        transitions_dir=transitions_dir,
        profile=profile,
    )
    entries: list[ScanEntry] = []
    skipped_unsupported = 0
    skipped_mounts = 0
    for root in inventory.roots:
        root_ancestry = _safe_root_ancestry(root)
        if root_ancestry is None:
            continue
        found, unsupported, mounts = _walk_root(
            root,
            root_device=root_ancestry[-1][1].device,
            root_ancestry=root_ancestry,
            attributed=inventory.attributed,
            excluded_roots=inventory.excluded_roots,
        )
        entries.extend(found)
        skipped_unsupported += unsupported
        skipped_mounts += mounts
    return ScanResult(
        entries=tuple(sorted(entries, key=lambda entry: str(entry.path))),
        skipped_unsupported=skipped_unsupported,
        skipped_mounts=skipped_mounts,
        unresolved=inventory.unresolved,
    )


def _managed_inventory(
    config: Config,
    repo_root: Path,
    *,
    config_path: Path,
    transitions_dir: Path,
    profile: str | None = None,
) -> _ManagedInventory:
    roots: set[Path] = set()
    unresolved_roots: set[Path] = set()
    attributed: set[Path] = set()
    unresolved: list[UnresolvedProfile] = []
    lost_ids: set[str] = set()
    for profile_name in config.profiles:
        effective_config = config.model_copy(deep=True)
        lost = (
            _without_unrenderable_bundle_files(effective_config, profile_name)
            if profile_name != profile
            else {}
        )
        if lost:
            unresolved.append(UnresolvedProfile(profile_name, "; ".join(lost.values())))
            lost_ids.update(lost)
        profile_roots = unresolved_roots if lost else roots
        effective = resolve_effective_profile(
            effective_config, profile_name, repo_root
        ).resolved
        if profile_name != profile:
            effective = _without_unrenderable(effective, effective_config)
        attributed.update(
            _norm(path)
            for path in codex_lifecycle.config_destinations(
                effective_config, effective, repo_root, profile=profile_name
            )
        )
        attributed.update(
            compare_mod._resolved_tracked_dsts(
                effective, effective_config, repo_root, extra_ids=frozenset()
            )
        )
        for tracked_id in effective.tracked_files:
            tracked_file = effective_config.tracked_files[tracked_id]
            src = compare_mod.resolve_src(tracked_file, repo_root)
            dst = _norm(compare_mod.resolve_dst(tracked_file))
            if src.is_dir():
                profile_roots.add(dst)
            else:
                root = _individual_file_root(dst)
                if root is not None:
                    profile_roots.add(root)
            if tracked_file.symlink is not None:
                link_target = _norm(Path(tracked_file.symlink))
                attributed.add(link_target)
                target_root = _individual_file_root(link_target)
                if target_root is not None:
                    profile_roots.add(target_root)

    attributed.update(_ignored_destinations(config, repo_root, transitions_dir))
    attributed.update(_recorded_destinations(lost_ids, transitions_dir))

    attributed.update(compare_mod._host_local_files(config))
    attributed.update(compare_mod._tracked_source_paths(config, repo_root))
    attributed.update(compare_mod._touched_paths_from_meta(transitions_dir))

    excluded_roots = (
        *(
            _norm(path)
            for path in (
                repo_root,
                config_path,
                paths.local_config_path(),
                state_root(),
                journals_root(),
                snapshots_root(),
            )
        ),
        *sorted(unresolved_roots - roots, key=str),
    )
    roots |= unresolved_roots
    bounded_roots = tuple(
        sorted(
            (
                root
                for root in roots
                if root not in _generic_roots()
                and not _is_excluded(root, excluded_roots)
            ),
            key=str,
        )
    )
    collapsed = tuple(
        root
        for root in bounded_roots
        if not any(
            root != other and root.is_relative_to(other) for other in bounded_roots
        )
    )
    return _ManagedInventory(
        roots=collapsed,
        attributed=frozenset(_norm(path) for path in attributed),
        excluded_roots=excluded_roots,
        unresolved=tuple(unresolved),
    )


def _ignored_destinations(
    config: Config, repo_root: Path, transitions_dir: Path
) -> set[Path]:
    return compare_mod.resolve_ignored_orphan_paths(
        compare_mod.load_ignored_orphans(), config, repo_root, transitions_dir
    )


def _without_unrenderable(
    effective: ResolvedProfile, config: Config
) -> ResolvedProfile:
    """Drop entries whose dst will not render, as they deploy nothing to scope.

    Applied to every profile but the selected one: a typo in a file only
    another profile uses must not stop this scan, while the selected profile
    keeps every entry and still raises on its own typo.
    """
    renderable: list[str] = []
    for name in effective.tracked_files:
        try:
            compare_mod.resolve_dst(config.tracked_files[name])
        except ConfigError:
            continue
        renderable.append(name)
    return effective.model_copy(update={"tracked_files": renderable})


def _without_unrenderable_bundle_files(config: Config, profile: str) -> dict[str, str]:
    """Drop the profile's bundle file components whose dst will not render.

    Returns each dropped component's synthetic tracked-file id with the reason.
    Where such a file was deployed is not known without rendering the dst, so
    the caller must not treat what it can no longer attribute as unrecorded.
    The profile's other entries resolve as usual.
    """
    lost: dict[str, str] = {}
    for bundle_id in resolve_profile(config, profile).bundles:
        bundle = config.bundles.get(bundle_id)
        if bundle is None:
            continue
        kept = []
        for component in bundle.components:
            synthetic_id = f"{bundle_id}.{component.id}"
            reason = _unrenderable_reason(synthetic_id, component.file)
            if reason is None:
                kept.append(component)
            else:
                lost[synthetic_id] = reason
        bundle.components = kept
    return lost


def _unrenderable_reason(
    synthetic_id: str, component: FileComponent | None
) -> str | None:
    if component is None or not component.template:
        return None
    try:
        paths.render_dst_template(component.dst)
    except TemplateError as exc:
        return (
            f"bundle file component {synthetic_id!r} dst {component.dst!r} is not "
            f"a renderable Jinja2 template: {exc}"
        )
    return None


def _recorded_destinations(ids: set[str], transitions_dir: Path) -> set[Path]:
    """Paths past installs and ownership claims recorded for these tracked ids."""
    if not ids:
        return set()
    return {
        _norm(path)
        for name, destinations in chain(
            compare_mod._recorded_file_destinations(transitions_dir),
            compare_mod._claimed_file_destinations(),
        )
        if any(
            name == tracked_id or name.startswith(f"{tracked_id}/")
            for tracked_id in ids
        )
        for path in destinations
    }


def _individual_file_root(path: Path) -> Path | None:
    candidates: list[Path] = []
    for parent in path.parents:
        if parent in _generic_roots() or parent == parent.parent:
            break
        candidates.append(parent)
    return candidates[-1] if candidates else None


def _generic_roots() -> frozenset[Path]:
    """Include import-time policy roots and the process's current isolated home."""
    home = _norm(Path.home())
    return frozenset(
        {
            *compare_mod.GENERIC_DST_ROOTS,
            home,
            home / ".config",
            home / ".local",
            home / ".local" / "state",
            home / ".local" / "share",
            home / ".cache",
        }
    )


def _norm(path: Path) -> Path:
    absolute = os.fspath(path.expanduser().absolute())
    return Path(os.path.normpath(f"/{absolute.lstrip('/')}"))


def _is_excluded(path: Path, controls: tuple[Path, ...]) -> bool:
    return any(path == control or path.is_relative_to(control) for control in controls)


def _safe_root_ancestry(
    root: Path, *, follow_root: bool = False
) -> tuple[tuple[Path, PathIdentity], ...] | None:
    """Capture every lexical root ancestor, resolving only symlinks above it.

    A symlinked ancestor is recorded by the directory it resolves to, with a
    symlink file type so the later walk follows that one component only.
    """
    ancestry: list[tuple[Path, PathIdentity]] = []
    for current in reversed((root, *root.parents[:-1])):
        try:
            info = current.lstat()
            alias = stat.S_ISLNK(info.st_mode) and (follow_root or current != root)
            if alias:
                info = current.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SetforgeError(f"managed scan root changed: {current}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise SetforgeError(
                f"refusing managed scan through non-directory or symlink: {current}"
            )
        identity = PathIdentity.from_stat(info)
        if alias:
            identity = replace(identity, mode=operations.alias_guard_mode(info.st_mode))
        ancestry.append((current, identity))
    return tuple(ancestry)


def _walk_root(
    root: Path,
    *,
    root_device: int,
    root_ancestry: tuple[tuple[Path, PathIdentity], ...],
    attributed: frozenset[Path],
    excluded_roots: tuple[Path, ...],
) -> tuple[list[ScanEntry], int, int]:
    entries: list[ScanEntry] = []
    skipped_unsupported = 0
    skipped_mounts = 0
    pending: list[tuple[Path, tuple[tuple[Path, PathIdentity], ...]]] = [
        (root, root_ancestry)
    ]
    while pending:
        directory, parent_identities = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda child: child.name)
        except OSError as exc:
            raise SetforgeError(
                f"cannot scan managed directory {directory}: {exc}"
            ) from exc
        for child in children:
            outcome = _inspect_child(
                child,
                root_device=root_device,
                parent_identities=parent_identities,
                attributed=attributed,
                excluded_roots=excluded_roots,
            )
            if outcome is _WalkSkip.MOUNT:
                skipped_mounts += 1
            elif outcome is _WalkSkip.UNSUPPORTED:
                skipped_unsupported += 1
            elif isinstance(outcome, ScanEntry):
                entries.append(outcome)
            elif outcome is not None:
                pending.append(outcome)
    return entries, skipped_unsupported, skipped_mounts


def _inspect_child(
    child: os.DirEntry[str],
    *,
    root_device: int,
    parent_identities: tuple[tuple[Path, PathIdentity], ...],
    attributed: frozenset[Path],
    excluded_roots: tuple[Path, ...],
) -> ScanEntry | tuple[Path, tuple[tuple[Path, PathIdentity], ...]] | _WalkSkip | None:
    path = _norm(Path(child.path))
    if _is_excluded(path, excluded_roots):
        return None
    try:
        info = child.stat(follow_symlinks=False)
    except OSError as exc:
        raise SetforgeError(f"managed scan entry changed: {path}") from exc
    identity = PathIdentity.from_stat(info)
    if stat.S_ISDIR(info.st_mode):
        if info.st_dev != root_device:
            return _WalkSkip.MOUNT
        return path, (*parent_identities, (path, identity))
    if path in attributed:
        return None
    classified = _classify_leaf(path, info)
    if classified is None:
        return _WalkSkip.UNSUPPORTED
    kind, link_target = classified
    try:
        after = path.lstat()
    except OSError as exc:
        raise SetforgeError(f"managed scan entry changed: {path}") from exc
    if identity != PathIdentity.from_stat(after):
        raise SetforgeError(f"managed scan entry changed: {path}")
    return ScanEntry(
        path=path,
        kind=kind,
        identity=identity,
        parent_identities=parent_identities,
        link_target=link_target,
    )


def _classify_leaf(
    path: Path, info: os.stat_result
) -> tuple[ScanEntryKind, str | None] | None:
    if stat.S_ISREG(info.st_mode):
        return ScanEntryKind.REGULAR, None
    if not stat.S_ISLNK(info.st_mode):
        return None
    try:
        return ScanEntryKind.SYMLINK, str(path.readlink())
    except OSError as exc:
        raise SetforgeError(f"managed scan symlink changed: {path}") from exc


def capture_parent_path_guards(
    paths: tuple[Path, ...],
) -> tuple[operations.PathGuard, ...]:
    """Capture stable lexical directory ancestors for journal recovery.

    A symlinked ancestor outside SetForge's own state trees is the operator's
    environment: it is guarded by the identity of the directory it resolves to.
    """
    state_trees = _state_trees()
    guards: dict[Path, operations.PathGuard] = {}
    for path in paths:
        for parent in path.expanduser().absolute().parents:
            if parent == Path("/"):
                break
            try:
                info = parent.lstat()
            except FileNotFoundError:
                guards[parent] = operations.PathGuard(parent, None, None, None)
                continue
            except OSError as exc:
                raise SetforgeError(f"cleanup parent changed: {parent}") from exc
            alias = stat.S_ISLNK(info.st_mode) and not any(
                parent != tree and parent.is_relative_to(tree) for tree in state_trees
            )
            if alias:
                try:
                    info = parent.stat()
                except OSError as exc:
                    raise SetforgeError(
                        f"refusing cleanup through non-directory or symlink: {parent}"
                    ) from exc
            if not stat.S_ISDIR(info.st_mode):
                raise SetforgeError(
                    f"refusing cleanup through non-directory or symlink: {parent}"
                )
            guards[parent] = operations.PathGuard(
                parent,
                info.st_dev,
                info.st_ino,
                operations.alias_guard_mode(info.st_mode) if alias else info.st_mode,
            )
    return tuple(guards[path] for path in sorted(guards, key=str))


def _state_trees() -> frozenset[Path]:
    """Return SetForge's state roots, lexical and resolved."""
    roots = (
        state_root(),
        journals_root(),
        snapshots_root(),
    )
    return frozenset(
        tree for root in roots for tree in (_norm(root), root.expanduser().resolve())
    )


def unlink_approved_entry(entry: ScanEntry) -> None:
    """Unlink the approved object relative to its identity-checked parent fd."""
    _with_verified_parent(entry, unlink=True)


def validate_approved_entry(entry: ScanEntry) -> None:
    """Refuse when an approved leaf or any lexical ancestor changed."""
    _with_verified_parent(entry, unlink=False)


def approval_matches(approved: ScanEntry, refreshed: ScanEntry) -> bool:
    """Match a frozen approval while ignoring directory timestamp churn."""
    if (
        approved.path,
        approved.kind,
        approved.identity,
        approved.link_target,
    ) != (
        refreshed.path,
        refreshed.kind,
        refreshed.identity,
        refreshed.link_target,
    ):
        return False
    approved_parents = {
        path: (identity.device, identity.inode, identity.mode)
        for path, identity in approved.parent_identities
    }
    refreshed_parents = {
        path: (identity.device, identity.inode, identity.mode)
        for path, identity in refreshed.parent_identities
    }
    return approved_parents == refreshed_parents


def freeze_candidate(path: Path) -> ScanEntry:
    """Freeze one regular file or symlink and every lexical parent identity."""
    path = _norm(path)
    ancestry = _safe_root_ancestry(path.parent, follow_root=True)
    if ancestry is None:
        raise SetforgeError(f"cleanup candidate parent is absent: {path.parent}")
    try:
        before = path.lstat()
    except OSError as exc:
        raise SetforgeError(f"cleanup candidate changed: {path}") from exc
    classified = _classify_leaf(path, before)
    if classified is None:
        raise SetforgeError(f"refusing unsupported cleanup candidate: {path}")
    kind, link_target = classified
    try:
        after = path.lstat()
    except OSError as exc:
        raise SetforgeError(f"cleanup candidate changed: {path}") from exc
    identity = PathIdentity.from_stat(before)
    if identity != PathIdentity.from_stat(after):
        raise SetforgeError(f"cleanup candidate changed: {path}")
    return ScanEntry(path, kind, identity, ancestry, link_target)


def _with_verified_parent(entry: ScanEntry, *, unlink: bool) -> None:
    identities: dict[Path, tuple[int, int, int] | None] = {
        path: (identity.device, identity.inode, identity.mode)
        for path, identity in entry.parent_identities
    }
    with operations.open_guarded_parent(
        entry.path,
        identities,
        create_missing=False,
        permit_existing_absent=False,
        changed="scan candidate parent changed",
        unguarded="scan candidate has no parent identity",
    ) as parent_fd:
        if parent_fd is None:  # pragma: no cover - every identity is present
            raise SetforgeError(f"scan candidate parent changed: {entry.path.parent}")
        try:
            current_info = os.stat(
                entry.path.name, dir_fd=parent_fd, follow_symlinks=False
            )
        except OSError as exc:
            raise SetforgeError(f"scan candidate changed: {entry.path}") from exc
        if PathIdentity.from_stat(current_info) != entry.identity:
            raise SetforgeError(f"scan candidate changed: {entry.path}")
        if not (
            stat.S_ISREG(current_info.st_mode) or stat.S_ISLNK(current_info.st_mode)
        ):
            raise SetforgeError(f"refusing unsupported scan candidate: {entry.path}")
        if unlink:
            os.unlink(entry.path.name, dir_fd=parent_fd)
