"""Atomic file deploy primitive with optional user-section and YAML
user-key preservation.

The deploy primitive is dotdrop's role reimplemented in stdlib + ruamel.yaml.
It writes a tracked file's content to its live destination atomically (via
``os.replace``), keeps a single ``.bak`` rotation per file, and offers two
preservation modes:

- ``preserve_user_sections``: HTML-comment marker regions (markdown).
- ``preserve_user_keys``: declarative JSONPath-lite list (YAML).

These compose: a single deploy may run YAML overlay first, then merge live
markdown sections into the result, though in practice a given dotfile is
either YAML or markdown.
"""

import contextlib
import errno
import io
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# ruamel.yaml ships py.typed but no usable annotations; no types-ruamel.yaml
# package on PyPI as of 2026-05.
from ruamel.yaml import YAML  # type: ignore[import-not-found]

from my_setup import jsonc, sections, yaml_merge
from my_setup.config import Config, ResolvedProfile
from my_setup.errors import MissingTrackedFile

LOGGER = logging.getLogger(__name__)


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
) -> DeployResult:
    """Atomically deploy ``src`` to ``dst``.

    When ``dst`` is a symlink the operation resolves to its target so the
    symlink itself is preserved (matches the legacy Makefile's behavior
    with ``link_dotfile_default: nolink``).

    When the resulting content is byte-identical to the existing ``dst``,
    no write or backup is performed (action == :attr:`DeployAction.NOOP`).
    """
    src = Path(src)
    dst = Path(str(dst)).expanduser()

    if not src.exists():
        raise MissingTrackedFile(f"tracked source not found: {src}")

    real_dst = dst.resolve() if dst.is_symlink() else dst
    real_dst.parent.mkdir(parents=True, exist_ok=True)
    dst_existed = real_dst.exists()

    content = _compute_content(
        src,
        real_dst,
        dst_existed,
        preserve_user_sections,
        preserve_user_keys,
        preserve_user_keys_deep,
    )

    if dst_existed:
        existing = real_dst.read_text(encoding="utf-8")
        action = DeployAction.NOOP if existing == content else DeployAction.UPDATED
    else:
        action = DeployAction.CREATED

    if action is DeployAction.NOOP:
        return DeployResult(dst=real_dst, action=action, backup_path=None)

    backup_path = _atomic_write(content, src, real_dst, dst_existed, backup)
    return DeployResult(dst=real_dst, action=action, backup_path=backup_path)


def _compute_content(
    src: Path,
    dst: Path,
    dst_existed: bool,
    preserve_user_sections: bool,
    preserve_user_keys: list[str] | None,
    preserve_user_keys_deep: list[str] | None = None,
) -> str:
    shallow = preserve_user_keys or []
    deep = preserve_user_keys_deep or []
    has_keys = bool(shallow or deep)
    if has_keys and dst_existed and jsonc.is_jsonc_file(src):
        tracked_text = src.read_text(encoding="utf-8")
        live_text = dst.read_text(encoding="utf-8")
        content = jsonc.overlay_user_keys(
            tracked_text, live_text, shallow, deep_key_names=deep
        )
    elif has_keys and dst_existed:
        yaml = YAML(typ="rt")
        with src.open("r", encoding="utf-8") as fh:
            src_doc = yaml.load(fh)
        with dst.open("r", encoding="utf-8") as fh:
            live_doc = yaml.load(fh)
        merged = yaml_merge.overlay(src_doc, live_doc, shallow, deep_key_paths=deep)
        buf = io.StringIO()
        yaml.dump(merged, buf)
        content = buf.getvalue()
    else:
        content = src.read_text(encoding="utf-8")

    if preserve_user_sections:
        live_sections: dict[str, str] = {}
        if dst_existed:
            live_text = dst.read_text(encoding="utf-8")
            live_sections = sections.extract_sections(live_text)
        content = sections.merge_sections(content, live_sections)

    return content


def _atomic_write(
    content: str,
    src: Path,
    dst: Path,
    dst_existed: bool,
    backup: bool,
) -> Path | None:
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(dst.parent), prefix=f".{dst.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        with contextlib.suppress(OSError):
            shutil.copystat(src, tmp_path)

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
    from my_setup.compare import resolve_src

    missing: list[str] = []
    for name in resolved.dotfiles:
        dotfile = cfg.dotfiles[name]
        src = resolve_src(dotfile, repo_root)
        if not src.exists():
            missing.append(f"{name}: {src}")
    if missing:
        joined = "\n  ".join(missing)
        raise MissingTrackedFile(f"missing tracked source(s):\n  {joined}")
