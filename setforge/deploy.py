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
import errno
import io
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ruamel.yaml import YAML

from setforge import host_local_inject, jsonc, sections, yaml_merge
from setforge.config import Config, ResolvedProfile, TrackedFile
from setforge.errors import MissingTrackedFile, SetforgeError
from setforge.section_reconcile import maintain_marker_hashes
from setforge.source import HostLocalSection, HostLocalSectionName

LOGGER: logging.Logger = logging.getLogger(__name__)


class DeployAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class DeployResult:
    dst: Path
    action: DeployAction
    backup_path: Path | None


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
) -> DeployResult:
    """Atomically deploy ``src`` to ``dst``.

    When ``dst`` is a symlink the operation resolves to its target so the
    symlink itself is preserved (matches the legacy Makefile's behavior
    with ``link_tracked_file_default: nolink``).

    When the resulting content is byte-identical to the existing ``dst``,
    no write or backup is performed (action == :attr:`DeployAction.NOOP`).

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
    """
    src = Path(src)
    dst = Path(str(dst)).expanduser()

    if not src.exists():
        raise MissingTrackedFile(f"tracked source not found: {src}")

    real_dst = _resolve_for_copy(dst)
    real_dst.parent.mkdir(parents=True, exist_ok=True)
    dst_existed = real_dst.exists()

    content = _compute_content(
        src,
        real_dst,
        dst_existed,
        preserve_user_sections,
        preserve_user_keys,
        preserve_user_keys_deep,
        section_bodies_override,
        precomputed_live_sections=precomputed_live_sections,
        host_local_sections=host_local_sections,
    )

    if dst_existed:
        existing = real_dst.read_text(encoding="utf-8")
        action = DeployAction.NOOP if existing == content else DeployAction.UPDATED
    else:
        action = DeployAction.CREATED

    if action is DeployAction.NOOP:
        return DeployResult(dst=real_dst, action=action, backup_path=None)

    backup_path = _atomic_write(content, src, real_dst, dst_existed, backup, mode)
    return DeployResult(dst=real_dst, action=action, backup_path=backup_path)


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
) -> str:
    """Render ``src`` with ``dst``'s shallow + deep user keys overlaid.

    Returns ``src`` verbatim when ``dst`` does not yet exist or no
    preserve-keys are configured. Otherwise dispatches on suffix:
    JSONC-family files go through :func:`jsonc.overlay_user_keys`;
    everything else is treated as YAML and routed through
    :func:`yaml_merge.overlay`. User-section merging is the next step in
    :func:`_compute_content` and is upstream of this helper's concern.
    """
    shallow = preserve_user_keys or []
    deep = preserve_user_keys_deep or []
    if not (dst_existed and (shallow or deep)):
        return src.read_text(encoding="utf-8")
    if jsonc.is_jsonc_file(src):
        tracked_text = src.read_text(encoding="utf-8")
        live_text = dst.read_text(encoding="utf-8")
        return jsonc.overlay_user_keys(
            tracked_text, live_text, shallow, deep_key_names=deep
        )
    yaml = YAML(typ="rt")
    with src.open("r", encoding="utf-8") as fh:
        src_doc = yaml.load(fh)
    with dst.open("r", encoding="utf-8") as fh:
        live_doc = yaml.load(fh)
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
) -> str:
    """Render the bytes ``copy_atomic`` will write to ``dst``.

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
    content = _render_with_preserve_keys(
        src,
        dst,
        dst_existed=dst_existed,
        preserve_user_keys=preserve_user_keys,
        preserve_user_keys_deep=preserve_user_keys_deep,
    )

    if preserve_user_sections:
        live_sections: dict[str, str]
        if precomputed_live_sections is not None:
            live_sections = dict(precomputed_live_sections)
        else:
            live_sections = {}
            if dst_existed:
                live_text = dst.read_text(encoding="utf-8")
                # allow_legacy=True so pre-9by live files (untagged markers,
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
        # the post-install hash invariant per setforge-xsco anti-smell #7.
        if host_local_sections:
            content = host_local_inject.inject_all(content, host_local_sections)
        # Post-merge: rewrite every end-marker hash to match the body
        # actually written. Idempotent + cheap; satisfies the post-install
        # invariant extract_marker_hashes(content) == hash_sections(content).
        content = maintain_marker_hashes(content)

    return content


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
            try:
                os.replace(dst, backup_path)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    shutil.copy2(dst, backup_path)
                    dst.unlink()
                else:
                    raise

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
        # m3qx symlink_target overlay (setforge-m3qx) surfaces the
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
    content = _compute_content(
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
