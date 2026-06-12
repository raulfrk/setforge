"""Atomic file deploy primitive with disposition merge + span preservation.

The deploy primitive is dotdrop's role reimplemented in stdlib + ruamel.yaml.
It writes a tracked file's content to its live destination atomically (via
``os.replace``), keeps a single ``.bak`` rotation per file, and reconciles
sub-file preservation through the unified model:

- ``disposition`` + ``base_text``: the stored-base 3-way merge driver
  (:mod:`setforge.disposition_merge`).
- ``spans``: PINNED / FORKED structural & markdown span re-overlay, and
  markerless host-local OVERLAY bodies (:mod:`setforge.overlay_deploy`).

The legacy ``preserve_user_sections`` / ``preserve_user_keys`` 2-way paths were
retired at schema 2.0 in favor of the above (see the contract migration
:mod:`setforge.migrations._contract_2_0`).
"""

import contextlib
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from setforge import (
    disposition_merge,
    host_local_inject,
    overlay_deploy,
    sections,
)
from setforge.config import Config, Disposition, ResolvedProfile, TrackedFile
from setforge.errors import MissingTrackedFile, SetforgeError
from setforge.markdown_merge import LineConflict
from setforge.section_reconcile import maintain_marker_hashes
from setforge.section_wizard import ReconcileAuto
from setforge.source import HostLocalSection, HostLocalSectionName
from setforge.spans import SpanEntry, SpanKind
from setforge.spans_overlay import SpanOrphan, apply_spans
from setforge.spans_store import SpanState
from setforge.structural_merge import PathConflict

LOGGER: logging.Logger = logging.getLogger(__name__)


class DeployAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class DeployResult:
    """Outcome of a :func:`copy_atomic` call.

    ``new_base`` / ``merge_conflicts`` are populated on the disposition
    (byte-base 3-way) path; ``new_span_states`` / ``span_orphans`` ride the
    span re-overlay path. All are inert defaults on a plain verbatim deploy.
    """

    dst: Path
    action: DeployAction
    backup_path: Path | None
    new_base: str | None = None
    merge_conflicts: list[LineConflict | PathConflict] = field(default_factory=list)
    new_span_states: dict[str, SpanState] | None = None
    span_orphans: list[SpanOrphan] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ResolvedDeploy:
    """The fully-computed, not-yet-written outcome of a deploy resolution.

    Produced by :func:`resolve_deploy` (pure read) and consumed by
    :func:`write_resolved_deploy` (the only writer). Carries everything the
    write step needs: the post-merge / post-overlay ``content``, the
    symlink-resolved ``real_dst`` plus its ``dst_existed`` probe, the
    ``effective_mode`` to apply, and the state-advance payload
    (``new_base`` / ``merge_conflicts`` / ``new_span_states`` /
    ``span_orphans``) that :class:`DeployResult` threads back to the caller.
    Holding these records in memory lets an orchestrator resolve EVERY file
    first and only then start writing (refuse-before-write).
    """

    src: Path
    real_dst: Path
    dst_existed: bool
    effective_mode: int | None
    content: str
    new_base: str | None
    merge_conflicts: list[LineConflict | PathConflict]
    new_span_states: dict[str, SpanState] | None
    span_orphans: list[SpanOrphan]


def _legacy_only_host_local(
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None,
    spans: list[SpanEntry] | None,
) -> dict[HostLocalSectionName, HostLocalSection] | None:
    """Drop host-local entries whose name is a host-local OVERLAY span anchor.

    The loader projects migrated OVERLAY spans back INTO the host-local map
    (:func:`setforge.source._host_local_sections_for_overlay`) so capture /
    compare / promote keep seeing the migrated bodies. On the deploy preserve
    path those names are injected MARKERLESS via
    :func:`setforge.overlay_deploy.inject_overlay_bodies`, so they must NOT also
    reach :func:`setforge.host_local_inject.inject_all` (which injects WITH
    markers — the double-injection trap). Returns the map unchanged when there
    are no overlay spans, so files with no overlay spans stay byte-for-byte
    untouched.
    """
    if not host_local_sections or not spans:
        return host_local_sections
    overlay_names = {s.anchor for s in overlay_deploy.overlay_spans(spans)}
    if not overlay_names:
        return host_local_sections
    return {
        name: section
        for name, section in host_local_sections.items()
        if name not in overlay_names
    }


def copy_atomic(
    src: Path,
    dst: Path,
    *,
    backup: bool = True,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
    mode: int | None = None,
    disposition: Disposition | None = None,
    base_text: str | None = None,
    merge_auto: ReconcileAuto | None = None,
    conflict_resolver: disposition_merge.ConflictResolver | None = None,
    spans: list[SpanEntry] | None = None,
    span_states: dict[str, SpanState] | None = None,
) -> DeployResult:
    """Atomically deploy ``src`` to ``dst``.

    Composes :func:`resolve_deploy` (the pure read: merge + span overlay
    computed in memory) with :func:`write_resolved_deploy` (the only write
    step). See :func:`resolve_deploy` for the full parameter contract; the
    two-step seam exists so an orchestrator can resolve every file before
    writing any.

    When ``dst`` is a symlink the operation resolves to its target so the
    symlink itself is preserved (matches the legacy Makefile's behavior
    with ``link_tracked_file_default: nolink``).

    When the resulting content is byte-identical to the existing ``dst``,
    no write or backup is performed (action == :attr:`DeployAction.NOOP`).
    """
    resolved = resolve_deploy(
        src,
        dst,
        host_local_sections=host_local_sections,
        mode=mode,
        disposition=disposition,
        base_text=base_text,
        merge_auto=merge_auto,
        conflict_resolver=conflict_resolver,
        spans=spans,
        span_states=span_states,
    )
    return write_resolved_deploy(resolved, backup=backup)


def resolve_deploy(
    src: Path,
    dst: Path,
    *,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
    mode: int | None = None,
    disposition: Disposition | None = None,
    base_text: str | None = None,
    merge_auto: ReconcileAuto | None = None,
    conflict_resolver: disposition_merge.ConflictResolver | None = None,
    spans: list[SpanEntry] | None = None,
    span_states: dict[str, SpanState] | None = None,
    live_text: str | None = None,
) -> ResolvedDeploy:
    """Compute a deploy's content + state advances WITHOUT writing anything.

    The read half of :func:`copy_atomic`: resolves ``dst`` through any
    pre-existing symlink, probes existence and effective mode, and runs the
    merge / span machinery entirely in memory. No directory is created and
    no file is touched — the returned :class:`ResolvedDeploy` is handed to
    :func:`write_resolved_deploy` when (and if) the caller decides to write.

    ``live_text``, when not ``None``, OVERRIDES the on-disk live content as
    the merge input. The caller uses this when a deferred live rewrite is
    pending (e.g. the disposition-base migration's SHARED-marker strip is
    computed in a read-only pass and applied later), so disk live ≠ the
    live the merge must see. ``dst_existed`` still reflects the disk probe.

    When ``disposition`` is not None the file is resolved via the
    stored-base 3-way merge driver
    (:func:`setforge.disposition_merge.resolve_file`): live (``dst``'s
    current bytes, ``""`` when absent), tracked (``src``'s bytes) and the
    stored ``base_text`` (the previously deployed base, ``None`` on first
    run) are merged under ``disposition`` + ``merge_auto`` (the install
    ``--auto``, threaded to the driver). The merged text becomes the
    resolution's ``content`` (the write step applies the NOOP/CREATED/UPDATED
    detection + :func:`_atomic_write` to it). The returned
    :class:`ResolvedDeploy` carries ``new_base`` (the bytes the caller should
    write to the stored base, or ``None`` to defer re-baselining) and
    ``merge_conflicts`` (every conflicting hunk/path, for the caller to
    warn — non-empty even when ``merge_auto`` resolved them). ``new_base``
    is computed from the driver resolution INDEPENDENTLY of the eventual
    write action: a clean merge whose result equals live is a NOOP write but
    still re-baselines (``new_base`` set). When ``disposition`` is None the
    file is deployed from ``src`` verbatim and ``new_base`` /
    ``merge_conflicts`` stay inert (``None`` / ``[]``). Symlinked
    tracked_files (deployed via the separate
    :func:`deploy_symlinked_file`, not this function) ignore ``disposition``
    for now.

    ``conflict_resolver`` is an OPTIONAL per-conflict resolver (a
    :data:`setforge.disposition_merge.ConflictResolver`) threaded into the
    disposition driver. When supplied AND a conflict arises (and
    ``merge_auto`` is None), each conflict is resolved by the resolver
    instead of the blanket policy — the interactive install builds a
    keyboard wizard here. ``merge_auto`` (``--auto``) takes precedence, so
    a non-``None`` auto resolves every conflict without consulting the
    resolver.

    ``spans`` are the file's sub-file span intents. Structural
    (yaml/json/jsonc) PINNED/FORKED spans are re-asserted inside the merge
    driver; markdown PINNED/FORKED spans use the text-splice re-overlay
    (:mod:`setforge.spans_overlay`); markerless host-local OVERLAY spans
    are excised before / injected after the merge
    (:mod:`setforge.overlay_deploy`). The OVERLAY path also runs on the
    ``disposition=None`` branch (a host-local-only file), where the content
    is otherwise tracked verbatim.

    ``host_local_sections`` is the legacy local.yaml ``host_local_sections``
    overlay (marker-injected via :func:`setforge.host_local_inject.inject_all`),
    a back-compat shim for hosts not yet rewritten to OVERLAY spans; names
    that are already OVERLAY span anchors are excluded here (injected
    markerless instead) to avoid double-injection.

    ``mode`` is the POSIX file-mode bits to apply to ``dst`` via
    ``os.fchmod`` on the temp fd BEFORE ``os.replace`` (closes the
    TOCTOU symlink-swap window and bypasses umask). When ``None``,
    the temp file inherits the source's mode via
    :func:`stat.S_IMODE` (today's behavior, zero regression).
    """
    src = Path(src)
    dst = Path(str(dst)).expanduser()

    if not src.exists():
        raise MissingTrackedFile(f"tracked source not found: {src}")

    real_dst = _resolve_for_copy(dst)
    dst_existed = real_dst.exists()

    # Effective write mode. ``mode`` (config ``mode:``) wins when set. On the
    # disposition path a re-baselined rewrite must NOT widen the existing live
    # mode toward the tracked source's (a live 0600 staying 0600), so when no
    # explicit mode is configured and a live file already exists, preserve its
    # mode rather than letting ``_atomic_write`` fall back to the source's mode.
    effective_mode = mode
    if effective_mode is None and disposition is not None and dst_existed:
        effective_mode = stat.S_IMODE(real_dst.stat().st_mode)
    if disposition is not None:
        content, new_base, merge_conflicts, new_span_states, span_orphans = (
            _resolve_disposition_content(
                src,
                real_dst,
                dst_existed,
                disposition,
                base_text,
                merge_auto,
                conflict_resolver,
                spans,
                span_states,
                live_text=live_text,
            )
        )
    else:
        content, new_span_states = _verbatim_with_overlay(
            src, host_local_sections, spans, span_states
        )
        new_base = None
        merge_conflicts = []
        span_orphans = []

    return ResolvedDeploy(
        src=src,
        real_dst=real_dst,
        dst_existed=dst_existed,
        effective_mode=effective_mode,
        content=content,
        new_base=new_base,
        merge_conflicts=merge_conflicts,
        new_span_states=new_span_states,
        span_orphans=span_orphans,
    )


def write_resolved_deploy(
    resolved: ResolvedDeploy, *, backup: bool = True
) -> DeployResult:
    """Write a :class:`ResolvedDeploy` to disk: the write half of :func:`copy_atomic`.

    Creates the destination's parent directories, then routes the resolved
    content through the shared NOOP/CREATED/UPDATED detection +
    :func:`_atomic_write` (see :func:`_write_resolved_content`). The
    resolution's state-advance payload rides through onto the returned
    :class:`DeployResult` unchanged.

    **Inter-resolve/write staleness assumption.** ``resolved`` snapshots the
    live file at :func:`resolve_deploy` time; an external edit to the live
    file between the resolve and this write is silently overwritten by the
    resolved content. setforge is a single-process CLI whose deploys are
    serialized under the profile lock, so the window is accepted and NOT
    re-checked here — the same single-setforge-process model documented for
    the symlink ordering window on :func:`deploy_symlinked_file`.
    """
    resolved.real_dst.parent.mkdir(parents=True, exist_ok=True)
    return _write_resolved_content(
        resolved.content,
        resolved.src,
        resolved.real_dst,
        resolved.dst_existed,
        backup,
        resolved.effective_mode,
        new_base=resolved.new_base,
        merge_conflicts=resolved.merge_conflicts,
        new_span_states=resolved.new_span_states,
        span_orphans=resolved.span_orphans,
    )


def _resolve_disposition_content(
    src: Path,
    real_dst: Path,
    dst_existed: bool,
    disposition: Disposition,
    base_text: str | None,
    merge_auto: ReconcileAuto | None,
    conflict_resolver: disposition_merge.ConflictResolver | None,
    spans: list[SpanEntry] | None,
    span_states: dict[str, SpanState] | None,
    *,
    live_text: str | None = None,
) -> tuple[
    str,
    str | None,
    list[LineConflict | PathConflict],
    dict[str, SpanState] | None,
    list[SpanOrphan],
]:
    """Run the stored-base 3-way merge + span re-overlay for a disposition file.

    Returns ``(content, new_base, merge_conflicts, new_span_states,
    span_orphans)``. Structural (yaml/json/jsonc) spans are re-asserted INSIDE
    the merge driver (the pin snapshot is taken from the FRESH live parse before
    the in-place merge); markdown PINNED/FORKED spans use the text-splice
    re-overlay; markerless host-local OVERLAY spans are excised BEFORE the merge
    and injected AFTER it (the body never enters base or tracked — leak gate).

    ``live_text`` overrides the on-disk live read when not ``None`` (see
    :func:`resolve_deploy`); every consumer of live content below — the merge
    driver, the OVERLAY excise, and the pinned-span re-overlay — sees the
    override.
    """
    if live_text is not None:
        live = live_text
    else:
        live = real_dst.read_text(encoding="utf-8") if dst_existed else ""
    tracked = src.read_text(encoding="utf-8")
    structural = disposition_merge.is_structural(real_dst)
    md_overlay_spans = (
        overlay_deploy.overlay_spans(spans) if (spans and not structural) else []
    )
    merge_spans = (
        [s for s in spans if s.kind is not SpanKind.OVERLAY] if spans else None
    )
    structural_spans = merge_spans if (merge_spans and structural) else None
    if md_overlay_spans:
        live, _ = overlay_deploy.excise_overlay_bodies(
            live, md_overlay_spans, span_states or {}
        )
    resolution = disposition_merge.resolve_file(
        disposition,
        real_dst,
        base_text,
        live,
        tracked,
        merge_auto,
        conflict_resolver,
        structural_spans=structural_spans,
        live_absent=not dst_existed,
    )
    content = resolution.text
    # new_base rides the resolution's advance decision, NOT the write action:
    # a clean merge whose result equals live is a NOOP write that still
    # re-baselines the stored base.
    new_base = resolution.text if resolution.advance_base else None
    merge_conflicts: list[LineConflict | PathConflict] = resolution.conflicts
    new_span_states: dict[str, SpanState] | None = None
    span_orphans: list[SpanOrphan] = []
    if resolution.structural_span_orphans:
        # Structural pins re-asserted inside the merge; the re-baseline already
        # used the post-reassert dump (B-S6). Surface any orphan through the
        # same warn machinery as markdown (anchor + kind).
        span_orphans = [
            SpanOrphan(anchor=o.anchor, kind=o.kind)
            for o in resolution.structural_span_orphans
        ]
    # Markdown span re-overlay (NEVER threaded into merge internals): splice
    # live bytes over each PINNED heading span AFTER the whole-file merge, then
    # re-baseline the byte base to the POST-splice bytes (Invariant I1). Forked
    # spans get no override but still recompute derived state for capture
    # exclusion. Skipped for structural files (handled inside the driver).
    if merge_spans and not structural:
        overlay = apply_spans(content, live, merge_spans, span_states or {})
        content = overlay.text
        new_span_states = overlay.new_states
        span_orphans = overlay.orphans
        if new_base is not None:
            new_base = content
    # OVERLAY inject runs LAST, on the body-free merged + pinned/forked content.
    # The base is re-baselined from the PRE-inject bytes (``new_base`` already
    # set), so the stored base stays body-free.
    if md_overlay_spans:
        injected, overlay_states = overlay_deploy.inject_overlay_bodies(
            content, md_overlay_spans, span_states or {}
        )
        content = injected
        new_span_states = {**(new_span_states or {}), **overlay_states}
    return content, new_base, merge_conflicts, new_span_states, span_orphans


def _verbatim_with_overlay(
    src: Path,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None,
    spans: list[SpanEntry] | None,
    span_states: dict[str, SpanState] | None,
) -> tuple[str, dict[str, SpanState] | None]:
    """Render a ``disposition=None`` file: tracked verbatim + host-local overlays.

    Returns ``(content, new_span_states)``. Host-local OVERLAY spans carry the
    per-host body markerless in local.yaml; the loader PROJECTS those into
    ``host_local_sections`` (for capture/compare/promote), so they are excluded
    from :func:`host_local_inject.inject_all` here (which injects WITH markers)
    and injected markerless below — else each body lands twice.
    """
    content = src.read_text(encoding="utf-8")
    new_span_states: dict[str, SpanState] | None = None
    md_overlay_spans = overlay_deploy.overlay_spans(spans) if spans else []
    legacy_host_local = _legacy_only_host_local(host_local_sections, spans)
    if legacy_host_local:
        content = host_local_inject.inject_all(content, legacy_host_local)
        content = maintain_marker_hashes(content)
    # De-marker + markerless inject: strip EVERY tracked-authored
    # host-local marker pair from the content, then splice each overlay body in
    # once. Runs only when host-local overlay spans are present, so files with
    # no host-local overlay stay byte-for-byte the tracked source.
    if md_overlay_spans:
        content = sections.strip_host_local_markers(content)
        content, new_span_states = overlay_deploy.inject_overlay_bodies(
            content, md_overlay_spans, span_states or {}
        )
    return content, new_span_states


def _write_resolved_content(
    content: str,
    src: Path,
    real_dst: Path,
    dst_existed: bool,
    backup: bool,
    mode: int | None,
    *,
    new_base: str | None,
    merge_conflicts: list[LineConflict | PathConflict],
    new_span_states: dict[str, SpanState] | None = None,
    span_orphans: list[SpanOrphan] | None = None,
) -> DeployResult:
    """Apply NOOP/CREATED/UPDATED detection + atomic write to ``content``.

    Shared by both branches of :func:`copy_atomic` so the NOOP-detection,
    mode-only-drift fixup and :func:`_atomic_write` logic live in one place.
    ``new_base`` / ``merge_conflicts`` (disposition path) and
    ``new_span_states`` / ``span_orphans`` (span re-overlay path) are threaded
    onto EVERY returned :class:`DeployResult` — including the NOOP and
    mode-only-drift paths — so a clean disposition merge that equals live still
    re-baselines and the spans sidecar advances even on a NOOP write whose
    post-splice content already equals live.
    """
    span_orphans = span_orphans or []
    if dst_existed:
        existing = real_dst.read_text(encoding="utf-8")
        action = DeployAction.NOOP if existing == content else DeployAction.UPDATED
    else:
        action = DeployAction.CREATED

    if action is DeployAction.NOOP:
        # Content matches, but mode bits may have drifted. compare flags
        # mode-only drift; apply it here (path-based chmod is safe — no
        # content swap to race, real_dst already symlink-resolved) so
        # install fixes perms instead of reporting "unchanged".
        if mode is not None and stat.S_IMODE(real_dst.stat().st_mode) != mode:
            os.chmod(real_dst, mode)
            return DeployResult(
                dst=real_dst,
                action=DeployAction.UPDATED,
                backup_path=None,
                new_base=new_base,
                merge_conflicts=merge_conflicts,
                new_span_states=new_span_states,
                span_orphans=span_orphans,
            )
        return DeployResult(
            dst=real_dst,
            action=action,
            backup_path=None,
            new_base=new_base,
            merge_conflicts=merge_conflicts,
            new_span_states=new_span_states,
            span_orphans=span_orphans,
        )

    backup_path = _atomic_write(content, src, real_dst, dst_existed, backup, mode)
    return DeployResult(
        dst=real_dst,
        action=action,
        backup_path=backup_path,
        new_base=new_base,
        merge_conflicts=merge_conflicts,
        new_span_states=new_span_states,
        span_orphans=span_orphans,
    )


def _resolve_for_copy(dst: Path) -> Path:
    """Resolve ``dst`` through any pre-existing symlink for legacy nolink copy.

    Mirrors the legacy ``link_tracked_file_default: nolink`` behavior:
    when ``dst`` is itself a symlink, write to its target (so the link
    survives the deploy). When :func:`Path.resolve` fails — broken
    link, dangling component, or :class:`RuntimeError` from cpython's
    symlink-loop detection — the original ``dst`` is returned and the
    caller treats it as a fresh write.

    ``strict=False`` is mandatory: ``Path.resolve(strict=True)`` raises
    :class:`OSError` on missing targets; ``strict=False`` swallows
    every :class:`OSError` EXCEPT the rare symlink-loop case (CPython
    bug #109187), which surfaces as :class:`RuntimeError`. The
    ``except (OSError, RuntimeError)`` covers both shapes so a hostile
    symlink layout can't crash deploy.
    """
    if not dst.is_symlink():
        return dst
    try:
        return dst.resolve(strict=False)
    except (OSError, RuntimeError):
        return dst


def _atomic_write(
    content: str,
    src: Path,
    dst: Path,
    dst_existed: bool,
    backup: bool,
    mode: int | None,
) -> Path | None:
    """Atomically write ``content`` to ``dst`` with explicit mode bits.

    ``os.fchmod`` runs on the temp fd BEFORE ``os.replace`` so the
    final mode is applied in the same FS object, closing the TOCTOU
    symlink-swap window that a post-replace path-based chmod call
    would expose. When ``mode`` is None, the temp file gets the
    source's mode (via :func:`stat.S_IMODE`) — today's behavior.
    fchmod failure is contractual: it propagates (no
    :func:`contextlib.suppress` wrapper).
    """
    effective_mode = mode if mode is not None else stat.S_IMODE(src.stat().st_mode)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(dst.parent), prefix=f".{dst.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fchmod(fh.fileno(), effective_mode)

        backup_path: Path | None = None
        if backup and dst_existed:
            backup_path = Path(str(dst) + ".bak")
            # Unlink any pre-existing .bak first: shutil.copy2 FOLLOWS a
            # symlink at the destination and would write dst's content
            # THROUGH it (clobbering the link target). The prior
            # rename-based backup replaced the link instead, so unlink to
            # preserve that "replace, never follow" semantics.
            with contextlib.suppress(FileNotFoundError):
                backup_path.unlink()
            # Copy (not rename) so dst stays in place until os.replace
            # atomically swaps the new content in — no window where dst is
            # absent. copy2 works across filesystems; tmp_path is always
            # in dst.parent, so os.replace below never hits EXDEV.
            shutil.copy2(dst, backup_path)

        os.replace(tmp_path, dst)
        return backup_path
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def deploy_symlinked_file(
    src: Path,
    dst: Path,
    tracked_file: TrackedFile,
    *,
    backup: bool = True,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
) -> DeployResult:
    """Deploy a tracked_file that declares ``symlink:``.

    Two-phase write:

    1. Render the tracked content to ``Path(tracked_file.symlink).expanduser()``
       (the target path) via :func:`_atomic_write` — the target file is
       the one actually carrying bytes.
    2. Create a symbolic link at ``dst`` pointing at the *raw user
       string* (``tracked_file.symlink``, NOT expanded) so cross-host
       portability survives. The link itself is staged at a sibling
       tempfile and ``os.replace``-d into place — the same atomic
       pattern :func:`_atomic_write` uses for regular files, closing
       the TOCTOU window between ``unlink`` and ``symlink``.

    Refusal contract: if ``dst`` already exists as a *regular file*
    (not a symlink), this function raises :class:`SetforgeError`. The
    caller should treat that case as drift requiring user intervention
    rather than silently clobbering local content. A pre-existing
    symlink at ``dst`` — regardless of where it points — is replaced
    atomically by :func:`os.replace`.

    Returns a :class:`DeployResult` mirroring :func:`copy_atomic`'s
    contract. ``backup_path`` is None for symlink deployments: the
    target-side write produces its own ``.bak`` for the byte content,
    and a link itself carries no rotateable state.

    Ordering window: target write precedes the link swap, so a
    concurrent reader following the *old* link (or the new link, if
    the dst path is racing with a sibling process) may briefly observe
    the new target bytes via the OLD link's path before this function
    swings the dst link onto its new target. Not exploitable in a
    security sense — the caller controls both paths — but worth
    knowing if a setforge install races with another tool reading the
    same tracked symlinks. Same-host single-setforge-process model
    serializes deploys, so this is theoretical for the canonical
    install/sync/revert flow. The same model covers the resolve→write
    staleness window on :func:`write_resolved_deploy` (an external live
    edit between the read-only resolve pass and the write pass is
    accepted, not re-checked).
    """
    if tracked_file.symlink is None:
        raise AssertionError(
            "deploy_symlinked_file called with tracked_file.symlink == None"
        )
    if not src.exists():
        raise MissingTrackedFile(f"tracked source not found: {src}")

    target = Path(tracked_file.symlink).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() and not dst.is_symlink():
        # Distinguish directory-at-dst from regular-file-at-dst so the
        # overlay-fields symlink_target overlay surfaces the
        # "directory in the way" case with a targeted message — silently
        # clobbering or recursing into a real directory layout is
        # almost certainly a config mistake.
        if dst.is_dir():
            raise SetforgeError(
                f"refusing to deploy symlink at {dst}: a directory is "
                f"already present. Move or remove it before deploying "
                f"tracked_file with symlink: {tracked_file.symlink!r}."
            )
        raise SetforgeError(
            f"refusing to deploy symlink at {dst}: a regular file is "
            f"already present. Move it aside or remove it before "
            f"deploying tracked_file with symlink: {tracked_file.symlink!r}."
        )

    _deploy_target_content(
        src,
        target,
        tracked_file,
        backup=backup,
        host_local_sections=host_local_sections,
    )
    action = _replace_symlink_atomic(dst, tracked_file.symlink)
    return DeployResult(dst=dst, action=action, backup_path=None)


def _deploy_target_content(
    src: Path,
    target: Path,
    tracked_file: TrackedFile,
    *,
    backup: bool,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
) -> None:
    """Write ``src`` content to ``target`` via :func:`_atomic_write`.

    The tracked content is written verbatim; the legacy
    ``host_local_sections`` overlay (marker-injected) still composes for
    symlink-deployed tracked_files via :func:`host_local_inject.inject_all`.
    ``mode`` rides through unchanged.
    """
    target_existed = target.exists()
    content = src.read_text(encoding="utf-8")
    if host_local_sections:
        content = host_local_inject.inject_all(content, host_local_sections)
        content = maintain_marker_hashes(content)
    _atomic_write(content, src, target, target_existed, backup, tracked_file.mode)


def _replace_symlink_atomic(dst: Path, raw_target: str) -> DeployAction:
    """Place a symlink at ``dst`` pointing at ``raw_target`` via tmp+replace.

    ``raw_target`` is the *unexpanded* user string (e.g. ``~/foo``);
    :func:`os.symlink` writes it verbatim into the link's metadata so
    a subsequent :func:`os.readlink` returns exactly that string —
    cross-host portability invariant. ``os.replace`` atomically swaps
    the staged link over any pre-existing link at ``dst`` (the
    regular-file case is refused by the caller).

    Fast-path: when ``dst`` is already a symlink with ``raw_target``
    verbatim, skip the tmp+replace dance entirely and return
    :attr:`DeployAction.NOOP` — a re-install of an already-correct
    link should not show ``UPDATED`` in the install summary nor
    spend an :func:`os.symlink` + :func:`os.replace` syscall pair.

    Returns :attr:`DeployAction.CREATED` when ``dst`` had no prior
    symlink, :attr:`DeployAction.NOOP` when the prior symlink already
    pointed at ``raw_target``, otherwise :attr:`DeployAction.UPDATED`.
    """
    if dst.is_symlink() and os.readlink(dst) == raw_target:
        return DeployAction.NOOP
    dst_was_link = dst.is_symlink()
    tmp_link = dst.parent / f".{dst.name}.setforge-symlink-tmp"
    with contextlib.suppress(FileNotFoundError):
        tmp_link.unlink()
    os.symlink(raw_target, tmp_link)
    os.replace(tmp_link, dst)
    return DeployAction.UPDATED if dst_was_link else DeployAction.CREATED


def bootstrap_local(paths: list[Path]) -> None:
    """Ensure each host-local file exists with parent directories.

    Used for ``~/.claude/header.md``, ``~/.claude/additional-content.md``,
    and any other never-tracked-but-referenced file. Creates an empty
    file if missing; a no-op if present.
    """
    for raw in paths:
        path = Path(str(raw)).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
            LOGGER.info("created stub: %s", path)


def validate_srcs_exist(
    cfg: Config, resolved: ResolvedProfile, repo_root: Path
) -> None:
    """Pre-flight: every tracked ``src`` path in the resolved profile
    must exist on disk. Raises a single :class:`MissingTrackedFile`
    listing every missing path so ``install`` fails before any deploy
    or backup happens.
    """
    from setforge.compare import resolve_src

    missing: list[str] = []
    for name in resolved.tracked_files:
        tracked_file = cfg.tracked_files[name]
        src = resolve_src(tracked_file, repo_root)
        if not src.exists():
            missing.append(f"{name}: {src}")
    if missing:
        joined = "\n  ".join(missing)
        raise MissingTrackedFile(f"missing tracked source(s):\n  {joined}")
