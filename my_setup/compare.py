"""Drift compare for tracked → live deployments.

Two-axis classification:

- ``preserve_user_keys`` paths in YAML files mark drift that we *expect*
  (live overlays tracked on the next deploy, by design).
- Everything else is *unexpected* drift — what ``compare --check`` flags
  for CI and what Pillar 4's ``merge`` wizard exists to resolve.
"""

import difflib
import io
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from jinja2 import Template
from ruamel.yaml import YAML

from my_setup import sections, yaml_merge
from my_setup.config import Config, Dotfile, resolve_profile
from my_setup.paths import template_context


class CompareStatus(StrEnum):
    UNCHANGED = "unchanged"
    DRIFTED = "drifted"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class FileCompare:
    name: str
    status: CompareStatus
    diff: str
    expected_drift_keys: list[str]
    unexpected_drift_keys: list[str]


@dataclass(frozen=True, slots=True)
class CompareReport:
    entries: list[FileCompare]
    has_unexpected_drift: bool


def resolve_src(dotfile: Dotfile, repo_root: Path) -> Path:
    """Resolve a dotfile's ``src`` (relative to ``tracked/``) to an
    absolute path inside the repo."""
    return repo_root / "tracked" / dotfile.src


def resolve_dst(dotfile: Dotfile) -> Path:
    """Resolve a dotfile's ``dst`` template (if any) to an absolute path
    via Jinja2 + ``~`` expansion."""
    raw = dotfile.dst
    if dotfile.template:
        raw = Template(raw).render(**template_context())
    return Path(raw).expanduser()


def diff_file(
    src: Path,
    dst: Path,
    *,
    preserve_user_sections: bool = False,
    preserve_user_keys: list[str] | None = None,
) -> str:
    """Return the unified diff between ``src`` and ``dst``.

    When preservation is enabled the comparison renders the post-merge
    content (same merge sequence as :func:`my_setup.deploy.copy_atomic`)
    so preserved drift never shows in the diff body.
    """
    if not dst.exists():
        return ""
    rendered_src = _render_with_merges(
        src, dst, preserve_user_sections, preserve_user_keys
    )
    live_text = dst.read_text(encoding="utf-8")
    diff_lines = difflib.unified_diff(
        live_text.splitlines(keepends=True),
        rendered_src.splitlines(keepends=True),
        fromfile=str(dst),
        tofile=str(src),
    )
    return "".join(diff_lines)


def _render_with_merges(
    src: Path,
    dst: Path,
    preserve_user_sections: bool,
    preserve_user_keys: list[str] | None,
) -> str:
    if preserve_user_keys:
        yaml = YAML(typ="rt")
        with src.open("r", encoding="utf-8") as fh:
            src_doc = yaml.load(fh)
        with dst.open("r", encoding="utf-8") as fh:
            live_doc = yaml.load(fh)
        merged = yaml_merge.overlay(src_doc, live_doc, preserve_user_keys)
        buf = io.StringIO()
        yaml.dump(merged, buf)
        content = buf.getvalue()
    else:
        content = src.read_text(encoding="utf-8")

    if preserve_user_sections:
        live_sections = sections.extract_sections(
            dst.read_text(encoding="utf-8")
        )
        content = sections.merge_sections(content, live_sections)
    return content


def classify_yaml_drift(
    src: Path,
    dst: Path,
    preserve_user_keys: list[str],
) -> tuple[list[str], list[str]]:
    """Return ``(expected, unexpected)`` JSONPath-lite paths where ``src``
    and ``dst`` diverge.

    A diverged path is *expected* iff covered by some entry in
    ``preserve_user_keys`` (exact match or a parent path with ``[*]``/``[]``).
    Everything else is *unexpected*.
    """
    yaml = YAML(typ="rt")
    with src.open("r", encoding="utf-8") as fh:
        src_doc = yaml.load(fh)
    with dst.open("r", encoding="utf-8") as fh:
        live_doc = yaml.load(fh)

    diverged_paths = _diff_paths(src_doc, live_doc)
    preserve_prefixes = [_to_prefix(p) for p in preserve_user_keys]

    expected: list[str] = []
    unexpected: list[str] = []
    for path in diverged_paths:
        formatted = _format_path(path)
        if any(_is_prefix(prefix, path) for prefix in preserve_prefixes):
            expected.append(formatted)
        else:
            unexpected.append(formatted)
    return expected, unexpected


def _to_prefix(preserve_path: str) -> tuple[str, ...]:
    tokens = yaml_merge._parse_path(preserve_path)
    return tuple(name for _, name in tokens)


def _is_prefix(prefix: tuple[str, ...], path: tuple) -> bool:
    if len(path) < len(prefix):
        return False
    for prefix_step, path_step in zip(prefix, path, strict=False):
        if isinstance(path_step, int):
            return False
        if path_step != prefix_step:
            return False
    return True


def _diff_paths(src: object, live: object, prefix: tuple = ()) -> list[tuple]:
    if isinstance(src, Mapping) and isinstance(live, Mapping):
        diffs: list[tuple] = []
        for key in set(src) | set(live):
            if key not in src or key not in live:
                diffs.append(prefix + (key,))
                continue
            diffs.extend(_diff_paths(src[key], live[key], prefix + (key,)))
        return diffs
    if isinstance(src, list) and isinstance(live, list):
        diffs = []
        for i in range(max(len(src), len(live))):
            if i >= len(src) or i >= len(live):
                diffs.append(prefix + (i,))
                continue
            diffs.extend(_diff_paths(src[i], live[i], prefix + (i,)))
        return diffs
    if src != live:
        return [prefix]
    return []


def _format_path(path: tuple) -> str:
    out: list[str] = []
    for i, step in enumerate(path):
        if isinstance(step, int):
            out.append(f"[{step}]")
        elif i == 0:
            out.append(str(step))
        else:
            out.append(f".{step}")
    return "".join(out) or "<root>"


def compare_profile(
    config: Config,
    profile_name: str,
    repo_root: Path,
) -> CompareReport:
    """Build a :class:`CompareReport` for every dotfile in the resolved profile."""
    resolved = resolve_profile(config, profile_name)
    entries: list[FileCompare] = []
    has_unexpected = False

    for name in resolved.dotfiles:
        dotfile = config.dotfiles[name]
        src = resolve_src(dotfile, repo_root)
        dst = resolve_dst(dotfile)

        if not dst.exists():
            entries.append(
                FileCompare(
                    name=name,
                    status=CompareStatus.MISSING,
                    diff="",
                    expected_drift_keys=[],
                    unexpected_drift_keys=[],
                )
            )
            has_unexpected = True
            continue

        diff = diff_file(
            src,
            dst,
            preserve_user_sections=dotfile.preserve_user_sections,
            preserve_user_keys=dotfile.preserve_user_keys or None,
        )

        expected_keys: list[str] = []
        unexpected_keys: list[str] = []
        if dotfile.preserve_user_keys:
            expected_keys, unexpected_keys = classify_yaml_drift(
                src, dst, dotfile.preserve_user_keys
            )

        is_drifted = bool(diff) or bool(expected_keys) or bool(unexpected_keys)
        status = CompareStatus.DRIFTED if is_drifted else CompareStatus.UNCHANGED

        if diff:
            has_unexpected = True

        entries.append(
            FileCompare(
                name=name,
                status=status,
                diff=diff,
                expected_drift_keys=expected_keys,
                unexpected_drift_keys=unexpected_keys,
            )
        )

    return CompareReport(entries=entries, has_unexpected_drift=has_unexpected)
