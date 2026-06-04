"""Atomic file deploy primitive with optional user-section and YAML
user-key preservation.

The deploy primitive is dotdrop's role reimplemented in stdlib + ruamel.yaml.
It writes a tracked file's content to its live destination atomically (via
``os.replace``), keeps a single ``.bak`` rotation per file, and offers two
preservation modes:

- ``preserve_user_sections``: HTML-comment marker regions (markdown).
- ``preserve_user_keys``: declarative JSONPath-lite list (YAML).

These compose: a single deploy may run YAML overlay first, then merge live
markdown sections into the result, though in practice a given tracked_file is
either YAML or markdown.
"""

import contextlib
import io
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ruamel.yaml import YAML

from setforge import (
    disposition_merge,
    host_local_inject,
    jsonc,
    scalar_overlay,
    sections,
    yaml_merge,
)
from setforge.config import Config, Disposition, ResolvedProfile, TrackedFile
from setforge.errors import MissingTrackedFile, SetforgeError
from setforge.markdown_merge import LineConflict
from setforge.scalar_merge import ABSENT
from setforge.section_reconcile import maintain_marker_hashes
from setforge.section_wizard import ReconcileAuto
from setforge.source import HostLocalSection, HostLocalSectionName
from setforge.structural_merge import PathConflict

LOGGER: logging.Logger = logging.getLogger(__name__)


class DeployAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class DeployResult:
    """Outcome of a :func:`copy_atomic` call.

    ``new_base``, ``merge_conflicts``, ``new_scalar_bases``, and
    ``scalar_conflicts`` are populated by :func:`copy_atomic` and are
    non-inert only on the disposition (byte-base 3-way) or scalar-overlay
    paths respectively; both are inert on the legacy blind-overlay path.
    """

    dst: Path
    action: DeployAction
    backup_path: Path | None
    new_base: str | None = None
    merge_conflicts: list[LineConflict | PathConflict] = field(default_factory=list)
    new_scalar_bases: dict[str, object] | None = None
    scalar_conflicts: list[str] = field(default_factory=list)


def copy_atomic(
    src: Path,
    dst: Path,
    *,
    backup: bool = True,
    preserve_user_sections: bool = False,
    preserve_user_keys: list[str] | None = None,
    preserve_user_keys_deep: list[str] | None = None,
    section_bodies_override: dict[str, str] | None = None,
    precomputed_live_sections: sections.LiveSections | None = None,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
    mode: int | None = None,
    disposition: Disposition | None = None,
    base_text: str | None = None,
    merge_auto: ReconcileAuto | None = None,
    scalar_bases: dict[str, object] | None = None,
) -> DeployResult:
    """Atomically deploy ``src`` to ``dst``.

    When ``dst`` is a symlink the operation resolves to its target so the
    symlink itself is preserved (matches the legacy Makefile's behavior
    with ``link_tracked_file_default: nolink``).

    When the resulting content is byte-identical to the existing ``dst``,
    no write or backup is performed (action == :attr:`DeployAction.NOOP`).

    When ``disposition`` is not None the file is resolved via the
    stored-base 3-way merge driver
    (:func:`setforge.disposition_merge.resolve_file`) instead of the
    legacy preserve path: live (``dst``'s current bytes, ``""`` when
    absent), tracked (``src``'s bytes) and the stored ``base_text`` (the
    previously deployed base, ``None`` on first run) are merged under
    ``disposition`` + ``merge_auto`` (the install ``--auto``, threaded to
    the driver). The merged text flows through the same
    NOOP/CREATED/UPDATED detection + :func:`_atomic_write` as the legacy
    branch. The returned :class:`DeployResult` carries ``new_base`` (the
    bytes the caller should write to the stored base, or ``None`` to
    defer re-baselining) and ``merge_conflicts`` (every conflicting
    hunk/path, for the caller to warn — non-empty even when ``merge_auto``
    resolved them). ``new_base`` is computed from the driver resolution
    INDEPENDENTLY of the write action: a clean merge whose result equals
    live is a NOOP write but still re-baselines (``new_base`` set). When
    ``disposition`` is None the legacy preserve path runs byte-for-byte
    unchanged and ``new_base``/``merge_conflicts`` stay inert
    (``None`` / ``[]``). Symlinked tracked_files (deployed via the separate
    :func:`deploy_symlinked_file`, not this function) ignore ``disposition``
    for now.

    When ``preserve_user_sections`` is True, the rendered content has
    every end-marker's ``hash=<...>`` rewritten via
    :func:`setforge.section_reconcile.maintain_marker_hashes` so the
    embedded hashes always match the body actually written
    (post-install invariant). ``section_bodies_override`` lets callers
    (the install path's wizard) supply a per-section body that overrides
    what :func:`extract_sections` would pick up from the existing live
    file — used for the ``take-tracked`` / edit actions.

    ``precomputed_live_sections`` lets callers that already parsed the
    live file (e.g. the install loop, which classifies section drift
    before deploying) skip the re-read + re-parse inside
    :func:`_compute_content`. The :class:`~sections.LiveSections` NewType
    pins the contract that this value came from
    :func:`sections.extract_live_sections` (i.e.  ``allow_legacy=True``)
    for the current on-disk live file; behaviour is otherwise identical
    to the default ``None`` code path. ``section_bodies_override`` still
    wins per-key when both are supplied.

    ``mode`` is the POSIX file-mode bits to apply to ``dst`` via
    ``os.fchmod`` on the temp fd BEFORE ``os.replace`` (closes the
    TOCTOU symlink-swap window and bypasses umask). When ``None``,
    the temp file inherits the source's mode via
    :func:`stat.S_IMODE` (today's behavior, zero regression).

    ``scalar_bases`` upgrades the SHALLOW ``preserve_user_keys`` overlay
    from a blind live-wins splice to a stored-base 3-way merge (see
    :mod:`setforge.scalar_overlay`). It is mutually exclusive with
    ``disposition`` (a file uses one model or the other) and requires
    ``disposition is None`` and ``preserve_user_keys`` non-empty to have any
    effect. It maps each shallow path to its stored base value (a typed
    scalar, ``None`` for a stored ``null``, or
    :data:`setforge.scalar_merge.ABSENT` for no stored base). Two scenarios:

    - **dst exists** — the driver resolves every shallow scalar 3-way
      against ``merge_auto`` (the install ``--auto``) and the resolved
      values flow back into the legacy overlay pipeline, so deep keys
      (``preserve_user_keys_deep``), user-sections and non-preserved/new
      tracked keys keep their tracked-structured legacy behavior
      byte-for-byte.
    - **dst absent (first install)** — the file is written from tracked
      verbatim (legacy behaviour), and the shallow scalar values found in
      the tracked source are SEEDED as ``new_scalar_bases`` so the NEXT
      install has a stored ancestor to 3-way against.

    The returned :class:`DeployResult` carries ``new_scalar_bases`` (the
    paths whose stored base should advance — seeded on first install,
    resolved on subsequent installs, with deferred bare conflicts omitted)
    and ``scalar_conflicts`` (every path that conflicted, even when
    ``merge_auto`` resolved it). When ``scalar_bases is None`` the legacy
    blind overlay runs verbatim and both fields stay inert (``None`` /
    ``[]``) — so non-install callers and files without preserve keys are
    byte-for-byte unchanged.
    """
    src = Path(src)
    dst = Path(str(dst)).expanduser()

    if not src.exists():
        raise MissingTrackedFile(f"tracked source not found: {src}")

    real_dst = _resolve_for_copy(dst)
    real_dst.parent.mkdir(parents=True, exist_ok=True)
    dst_existed = real_dst.exists()

    new_base: str | None = None
    merge_conflicts: list[LineConflict | PathConflict] = []
    new_scalar_bases: dict[str, object] | None = None
    scalar_conflicts: list[str] = []
    if disposition is not None:
        live = real_dst.read_text(encoding="utf-8") if dst_existed else ""
        tracked = src.read_text(encoding="utf-8")
        resolution = disposition_merge.resolve_file(
            disposition, real_dst, base_text, live, tracked, merge_auto
        )
        content = resolution.text
        # new_base rides the resolution's advance decision, NOT the write
        # action: a clean merge whose result equals live is a NOOP write
        # that still re-baselines the stored base.
        new_base = resolution.text if resolution.advance_base else None
        merge_conflicts = resolution.conflicts
    else:
        content, new_scalar_bases, scalar_conflicts = _compute_content(
            src,
            real_dst,
            dst_existed,
            preserve_user_sections,
            preserve_user_keys,
            preserve_user_keys_deep,
            section_bodies_override,
            precomputed_live_sections=precomputed_live_sections,
            host_local_sections=host_local_sections,
            scalar_bases=scalar_bases,
            merge_auto=merge_auto,
        )

    return _write_resolved_content(
        content,
        src,
        real_dst,
        dst_existed,
        backup,
        mode,
        new_base=new_base,
        merge_conflicts=merge_conflicts,
        new_scalar_bases=new_scalar_bases,
        scalar_conflicts=scalar_conflicts,
    )


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
    new_scalar_bases: dict[str, object] | None = None,
    scalar_conflicts: list[str] | None = None,
) -> DeployResult:
    """Apply NOOP/CREATED/UPDATED detection + atomic write to ``content``.

    Shared by the legacy preserve branch and the disposition branch of
    :func:`copy_atomic` so the NOOP-detection, mode-only-drift fixup and
    :func:`_atomic_write` logic live in one place. ``new_base`` and
    ``merge_conflicts`` (disposition path) plus ``new_scalar_bases`` and
    ``scalar_conflicts`` (shallow scalar-overlay path) are threaded onto
    EVERY returned :class:`DeployResult` — including the NOOP and
    mode-only-drift paths — so a clean disposition merge that equals live
    still re-baselines and a scalar overlay whose result equals live still
    advances its per-path bases.
    """
    scalar_conflicts = scalar_conflicts or []
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
                new_scalar_bases=new_scalar_bases,
                scalar_conflicts=scalar_conflicts,
            )
        return DeployResult(
            dst=real_dst,
            action=action,
            backup_path=None,
            new_base=new_base,
            merge_conflicts=merge_conflicts,
            new_scalar_bases=new_scalar_bases,
            scalar_conflicts=scalar_conflicts,
        )

    backup_path = _atomic_write(content, src, real_dst, dst_existed, backup, mode)
    return DeployResult(
        dst=real_dst,
        action=action,
        backup_path=backup_path,
        new_base=new_base,
        merge_conflicts=merge_conflicts,
        new_scalar_bases=new_scalar_bases,
        scalar_conflicts=scalar_conflicts,
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


def _render_with_preserve_keys(
    src: Path,
    dst: Path,
    *,
    dst_existed: bool,
    preserve_user_keys: list[str] | None,
    preserve_user_keys_deep: list[str] | None,
    scalar_bases: dict[str, object] | None = None,
    merge_auto: ReconcileAuto | None = None,
) -> tuple[str, dict[str, object] | None, list[str]]:
    """Render ``src`` with ``dst``'s shallow + deep user keys overlaid.

    Returns ``(content, new_scalar_bases, scalar_conflicts)``. The latter
    two are non-inert only on the scalar-overlay path (below); the legacy
    path returns ``(content, None, [])``.

    Returns ``src`` verbatim when ``dst`` does not yet exist or no
    preserve-keys are configured. When ``dst`` exists and preserve-keys
    are declared, the suffix-dispatch (JSONC vs YAML) and deep-key merging
    are handled by :func:`_overlay_preserve_keys`. User-section merging is
    the next step in :func:`_compute_content` and is upstream of this
    helper's concern.

    When ``scalar_bases is not None`` and shallow ``preserve_user_keys``
    exist, the SHALLOW step is upgraded to a stored-base 3-way merge: the
    live text is first resolved path-by-path by
    :func:`setforge.scalar_overlay.resolve_scalar_overlay`, and that
    resolved text is then fed as the LIVE source into
    :func:`_overlay_preserve_keys`. When ``dst`` does NOT yet exist (true
    first install) the content is tracked verbatim — the legacy behaviour
    — but the deployed scalar values are seeded as ``new_scalar_bases`` so
    the NEXT install has a stored ancestor to 3-way against.
    """
    shallow = preserve_user_keys or []
    deep = preserve_user_keys_deep or []
    if not (dst_existed and (shallow or deep)):
        tracked_text = src.read_text(encoding="utf-8")
        seed: dict[str, object] | None = None
        if scalar_bases is not None and shallow and not dst_existed:
            # True first install: dst is created from tracked verbatim, so
            # seed each shallow scalar path's base to its tracked value
            # (non-scalar / absent leaves are skipped by the seeder).
            seed = scalar_overlay.seed_scalar_bases(dst, tracked_text, shallow)
        return tracked_text, seed, []

    tracked_text = src.read_text(encoding="utf-8")
    live_text = dst.read_text(encoding="utf-8")
    new_scalar_bases: dict[str, object] | None = None
    scalar_conflicts: list[str] = []
    if scalar_bases is not None and shallow:
        scalar_result = scalar_overlay.resolve_scalar_overlay(
            dst,
            live_text,
            tracked_text,
            shallow,
            lambda path: scalar_bases.get(path, ABSENT),
            merge_auto,
        )
        live_text = scalar_result.merged_text
        # Deferred bare conflicts are already omitted from ``rebaseline``
        # by the driver, so this map is exactly the set of paths whose
        # stored base the caller should advance.
        new_scalar_bases = scalar_result.rebaseline
        scalar_conflicts = scalar_result.conflicts

    content = _overlay_preserve_keys(src, tracked_text, live_text, shallow, deep)
    return content, new_scalar_bases, scalar_conflicts


def _overlay_preserve_keys(
    src: Path,
    tracked_text: str,
    live_text: str,
    shallow: list[str],
    deep: list[str],
) -> str:
    """Run the legacy shallow+deep preserve overlay of ``live_text`` onto tracked.

    Dispatches on suffix: JSONC-family files go through
    :func:`jsonc.overlay_user_keys`; everything else is treated as YAML and
    routed through :func:`yaml_merge.overlay`. ``live_text`` is the source
    of overlaid values — either ``dst``'s raw bytes (legacy blind path) or
    the scalar driver's 3-way-resolved text (scalar path); both produce a
    tracked-structured result, so the two paths share this body verbatim.
    """
    if jsonc.is_jsonc_file(src):
        return jsonc.overlay_user_keys(
            tracked_text, live_text, shallow, deep_key_names=deep
        )
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    src_doc = yaml.load(tracked_text)
    live_doc = yaml.load(live_text)
    merged = yaml_merge.overlay(src_doc, live_doc, shallow, deep_key_paths=deep)
    buf = io.StringIO()
    yaml.dump(merged, buf)
    return buf.getvalue()


def _compute_content(
    src: Path,
    dst: Path,
    dst_existed: bool,
    preserve_user_sections: bool,
    preserve_user_keys: list[str] | None,
    preserve_user_keys_deep: list[str] | None = None,
    section_bodies_override: dict[str, str] | None = None,
    *,
    precomputed_live_sections: sections.LiveSections | None = None,
    host_local_sections: dict[HostLocalSectionName, HostLocalSection] | None = None,
    scalar_bases: dict[str, object] | None = None,
    merge_auto: ReconcileAuto | None = None,
) -> tuple[str, dict[str, object] | None, list[str]]:
    """Render the bytes ``copy_atomic`` will write to ``dst``.

    Returns ``(content, new_scalar_bases, scalar_conflicts)`` — the latter
    two ride the shallow scalar-overlay path (see
    :func:`_render_with_preserve_keys`) and stay inert (``None`` / ``[]``)
    when ``scalar_bases is None``. The user-section merge runs on top of
    ``content`` exactly as before, untouched by the scalar wiring.

    When ``preserve_user_sections`` is True the function normally re-reads
    ``dst`` and calls :func:`sections.extract_sections` to recover the
    live bodies. Callers that already extracted those sections (the
    install loop, which classifies drift before deploying) may pass
    ``precomputed_live_sections`` (a :class:`~sections.LiveSections`,
    produced by :func:`sections.extract_live_sections`) to skip the
    re-read; the NewType pins ``allow_legacy=True`` semantics so the
    install-loop pre-extract and the in-deploy fallback stay in lockstep.
    ``section_bodies_override`` still layers on top per-key, matching the
    no-precompute path.

    Cache shape: takes ``precomputed_live_sections`` as a typed
    pre-extracted :class:`~sections.LiveSections` (not raw text)
    because deploy has no upstream need for raw live text — it only
    splices live bodies into tracked content and stamps end-marker
    hashes. The symmetric compare-side helper
    (:func:`setforge.compare._render_with_merges`) caches raw
    ``dst_text`` instead because compare's strip-template comparison
    needs the text. See also: that function's docstring for the
    symmetric rationale.
    """
    content, new_scalar_bases, scalar_conflicts = _render_with_preserve_keys(
        src,
        dst,
        dst_existed=dst_existed,
        preserve_user_keys=preserve_user_keys,
        preserve_user_keys_deep=preserve_user_keys_deep,
        scalar_bases=scalar_bases,
        merge_auto=merge_auto,
    )

    if preserve_user_sections:
        live_sections: dict[str, str]
        if precomputed_live_sections is not None:
            live_sections = dict(precomputed_live_sections)
        else:
            live_sections = {}
            if dst_existed:
                live_text = dst.read_text(encoding="utf-8")
                # allow_legacy=True so pre-hash live files (untagged markers,
                # no end-marker hash) migrate in place on first install: the
                # subsequent merge_sections + maintain_marker_hashes pipeline
                # emits a fully-tagged, hash-stamped live file. The
                # precomputed path is already legacy-tolerant because the
                # install loop pre-extracts with allow_legacy=True too.
                live_sections = sections.extract_sections(live_text, allow_legacy=True)
        if section_bodies_override:
            # Per-section override from the install-time wizard: takes
            # precedence over the live body for sections the wizard
            # resolved (e.g. take-tracked, edit). Sections not in the
            # override map fall through to live-as-is.
            live_sections = {**live_sections, **section_bodies_override}
        content = sections.merge_sections(content, live_sections)
        # Inject host-local user-sections from local.yaml AFTER merge_sections
        # (so anchors resolve against post-merge content with live bodies)
        # but BEFORE maintain_marker_hashes (so the new pairs' end markers
        # are stamped with the canonical hash). Outside this window breaks
        # the post-install hash invariant per anti-smell #7.
        if host_local_sections:
            content = host_local_inject.inject_all(content, host_local_sections)
        # Post-merge: rewrite every end-marker hash to match the body
        # actually written. Idempotent + cheap; satisfies the post-install
        # invariant extract_marker_hashes(content) == hash_sections(content).
        content = maintain_marker_hashes(content)

    return content, new_scalar_bases, scalar_conflicts


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
    install/sync/revert flow.
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

    Routes through the same content-render path as :func:`copy_atomic`
    so preserve_user_keys / preserve_user_sections still compose with
    symlink-deployed tracked_files. ``mode`` rides through unchanged.
    """
    target_existed = target.exists()
    # Symlink-deployed files never wire the scalar-overlay path (the stored
    # base lifecycle is regular-file-only), so the scalar tuple fields are
    # discarded here — the legacy preserve overlay runs exactly as before.
    content, _new_scalar_bases, _scalar_conflicts = _compute_content(
        src,
        target,
        target_existed,
        tracked_file.preserve_user_sections,
        tracked_file.preserve_user_keys or None,
        tracked_file.preserve_user_keys_deep or None,
        None,
        host_local_sections=host_local_sections,
    )
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
