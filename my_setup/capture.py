"""Capture: live → tracked.

The inverse of ``deploy.copy_atomic``. Reads each profile dotfile's
``dst`` (the live copy) and writes a stripped version back to ``src``
(the tracked copy):

- ``preserve_user_sections`` files have the content between markers
  emptied (markers themselves remain, ready for a future deploy).
- ``preserve_user_keys`` files have those YAML keys removed (so live
  values stay host-local and never bake into the repo).
"""

import io
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ruamel.yaml import YAML

from my_setup import jsonc, sections, yaml_merge
from my_setup.compare import expand_dotfile, resolve_dst, resolve_src
from my_setup.config import Config, SectionMode, resolve_profile


class CaptureAction(StrEnum):
    UPDATED = "updated"
    NOOP = "noop"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CaptureResult:
    name: str
    action: CaptureAction
    reason: str = ""


def capture_dotfile(
    src: Path,
    dst: Path,
    *,
    preserve_user_sections: bool,
    preserve_user_keys: list[str],
    preserve_user_sections_mode: SectionMode = SectionMode.KEEP_DEFAULTS,
) -> CaptureResult:
    """Write a stripped version of ``dst`` (live) back to ``src`` (tracked).

    Empty ``preserve_user_keys`` and ``preserve_user_sections`` mean a
    direct copy. Returns :class:`CaptureResult.NOOP` if the resulting
    tracked content is byte-identical to the existing tracked file.

    ``preserve_user_sections_mode`` decides whether marker bodies in
    tracked are preserved (``KEEP_DEFAULTS``, default) or wiped
    (``STRIP``). KEEP_DEFAULTS falls back to STRIP semantics when src
    doesn't yet exist — no defaults to preserve.
    """
    if not dst.exists():
        return CaptureResult(name=src.name, action=CaptureAction.SKIPPED, reason="live missing")

    if preserve_user_keys and jsonc.is_jsonc_file(dst):
        live_text = dst.read_text(encoding="utf-8")
        content = jsonc.strip_user_keys(live_text, preserve_user_keys)
    elif preserve_user_keys:
        yaml = YAML(typ="rt")
        with dst.open("r", encoding="utf-8") as fh:
            doc = yaml.load(fh)
        yaml_merge.delete_keys(doc, preserve_user_keys)
        buf = io.StringIO()
        yaml.dump(doc, buf)
        content = buf.getvalue()
    else:
        content = dst.read_text(encoding="utf-8")

    if preserve_user_sections:
        if (
            preserve_user_sections_mode is SectionMode.KEEP_DEFAULTS
            and src.exists()
        ):
            tracked_text = src.read_text(encoding="utf-8")
            tracked_sections = sections.extract_sections(tracked_text)
            content = sections.merge_sections(content, tracked_sections)
        else:
            content = sections.strip_section_content(content)

    src.parent.mkdir(parents=True, exist_ok=True)
    if src.exists() and src.read_text(encoding="utf-8") == content:
        return CaptureResult(name=src.name, action=CaptureAction.NOOP)

    src.write_text(content, encoding="utf-8")
    return CaptureResult(name=src.name, action=CaptureAction.UPDATED)


def capture_profile(
    config: Config,
    profile_name: str,
    repo_root: Path,
) -> list[CaptureResult]:
    """Capture every dotfile in the resolved profile from live → tracked."""
    resolved = resolve_profile(config, profile_name)
    results: list[CaptureResult] = []
    for name in resolved.dotfiles:
        dotfile = config.dotfiles[name]
        src = resolve_src(dotfile, repo_root)
        dst = resolve_dst(dotfile)
        for sub_name, sub_src, sub_dst in expand_dotfile(name, src, dst):
            result = capture_dotfile(
                sub_src,
                sub_dst,
                preserve_user_sections=dotfile.preserve_user_sections,
                preserve_user_keys=dotfile.preserve_user_keys,
                preserve_user_sections_mode=dotfile.preserve_user_sections_mode,
            )
            results.append(
                CaptureResult(
                    name=sub_name, action=result.action, reason=result.reason
                )
            )
    return results
