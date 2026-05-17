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
from rich.table import Table

# ruamel.yaml ships py.typed without resolvable annotations; no stub pkg on PyPI.
from ruamel.yaml import YAML  # type: ignore[import-not-found]

from my_setup import jsonc, sections, yaml_merge
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
    preserve_user_keys_deep: list[str] | None = None,
) -> str:
    """Return the unified diff between ``src`` and ``dst``.

    When preservation is enabled the comparison renders the post-merge
    content (same merge sequence as :func:`my_setup.deploy.copy_atomic`)
    so preserved drift never shows in the diff body.

    Fast path: with ``preserve_user_sections=True``, if every section's
    sha256 matches between src and dst AND the non-section content is
    byte-identical, the rendered merge would equal live — skip the
    splice + diff and return ``""`` early.
    """
    if not dst.exists():
        return ""

    dst_text = dst.read_text(encoding="utf-8")
    if preserve_user_sections:
        src_text = src.read_text(encoding="utf-8")
        # Live side is parsed with allow_legacy=True so install's
        # pre-deploy compare step survives a pre-9by user file. The
        # compare CLI command surfaces a user-actionable error via
        # ``cli._refuse_legacy_live_markers`` BEFORE reaching here when
        # invoked directly; this branch is reached only from install's
        # drift gate, where lenience is correct.
        bodies_match = sections.hash_sections(src_text) == sections.hash_sections(
            dst_text, allow_legacy=True
        )
        template_matches = sections.strip_section_content(
            src_text, allow_legacy=True
        ) == sections.strip_section_content(dst_text, allow_legacy=True)
        if bodies_match and template_matches:
            return ""

    rendered_src = _render_with_merges(
        src,
        dst,
        preserve_user_sections,
        preserve_user_keys,
        preserve_user_keys_deep,
        dst_text=dst_text,
    )
    diff_lines = difflib.unified_diff(
        dst_text.splitlines(keepends=True),
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
    preserve_user_keys_deep: list[str] | None = None,
    *,
    dst_text: str,
) -> str:
    shallow = preserve_user_keys or []
    deep = preserve_user_keys_deep or []
    if (shallow or deep) and jsonc.is_jsonc_file(src):
        tracked_text = src.read_text(encoding="utf-8")
        live_text = dst.read_text(encoding="utf-8")
        content = jsonc.overlay_user_keys(
            tracked_text, live_text, shallow, deep_key_names=deep
        )
    elif shallow or deep:
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
        # See ``diff_file`` above for the ``allow_legacy=True`` rationale.
        live_sections = sections.extract_sections(dst_text, allow_legacy=True)
        content = sections.merge_sections(content, live_sections)
    return content


def classify_yaml_drift(
    src: Path,
    dst: Path,
    preserve_user_keys: list[str],
    preserve_user_keys_deep: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return ``(expected, unexpected)`` JSONPath-lite paths where ``src``
    and ``dst`` diverge.

    A diverged path is *expected* iff covered by some entry in
    ``preserve_user_keys`` (shallow whole-leaf overlay, exact match or
    a parent path with ``[*]``/``[]``) OR by some entry in
    ``preserve_user_keys_deep`` (deep-merge overlay; any sub-path
    beneath a deep entry classifies as expected because deploy
    reconciles them at deep-merge time). Everything else is *unexpected*.
    """
    yaml = YAML(typ="rt")
    with src.open("r", encoding="utf-8") as fh:
        src_doc = yaml.load(fh)
    with dst.open("r", encoding="utf-8") as fh:
        live_doc = yaml.load(fh)

    diverged_paths = _diff_paths(src_doc, live_doc)
    preserve_prefixes = [_to_prefix(p) for p in preserve_user_keys]
    preserve_prefixes.extend(_to_prefix(p) for p in preserve_user_keys_deep or [])

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
                diffs.append((*prefix, key))
                continue
            diffs.extend(_diff_paths(src[key], live[key], (*prefix, key)))
        return diffs
    if isinstance(src, list) and isinstance(live, list):
        diffs = []
        for i in range(max(len(src), len(live))):
            if i >= len(src) or i >= len(live):
                diffs.append((*prefix, i))
                continue
            diffs.extend(_diff_paths(src[i], live[i], (*prefix, i)))
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


def expand_dotfile(name: str, src: Path, dst: Path) -> list[tuple[str, Path, Path]]:
    """Expand a dotfile into ``(name, src_file, dst_file)`` triples.

    Plain files yield a single triple; directories yield one triple per
    contained file with a ``name/relpath`` synthetic name.
    """
    if src.is_dir():
        triples: list[tuple[str, Path, Path]] = []
        for file in sorted(src.rglob("*")):
            if file.is_file():
                rel = file.relative_to(src)
                triples.append((f"{name}/{rel}", file, dst / rel))
        return triples
    return [(name, src, dst)]


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

        for sub_name, sub_src, sub_dst in expand_dotfile(name, src, dst):
            entry, sub_unexpected = _compare_one(sub_name, sub_src, sub_dst, dotfile)
            entries.append(entry)
            if sub_unexpected:
                has_unexpected = True

    return CompareReport(entries=entries, has_unexpected_drift=has_unexpected)


def _compare_one(
    name: str, src: Path, dst: Path, dotfile: Dotfile
) -> tuple[FileCompare, bool]:
    if not dst.exists():
        return (
            FileCompare(
                name=name,
                status=CompareStatus.MISSING,
                diff="",
                expected_drift_keys=[],
                unexpected_drift_keys=[],
            ),
            True,
        )

    diff = diff_file(
        src,
        dst,
        preserve_user_sections=dotfile.preserve_user_sections,
        preserve_user_keys=dotfile.preserve_user_keys or None,
        preserve_user_keys_deep=dotfile.preserve_user_keys_deep or None,
    )

    expected_keys: list[str] = []
    unexpected_keys: list[str] = []
    if dotfile.preserve_user_keys or dotfile.preserve_user_keys_deep:
        if jsonc.is_jsonc_file(src):
            expected_keys, unexpected_keys = jsonc.classify_jsonc_drift(
                src.read_text(encoding="utf-8"),
                dst.read_text(encoding="utf-8"),
                dotfile.preserve_user_keys,
                deep_key_names=dotfile.preserve_user_keys_deep,
            )
        else:
            expected_keys, unexpected_keys = classify_yaml_drift(
                src,
                dst,
                dotfile.preserve_user_keys,
                preserve_user_keys_deep=dotfile.preserve_user_keys_deep,
            )

    is_drifted = bool(diff) or bool(expected_keys) or bool(unexpected_keys)
    status = CompareStatus.DRIFTED if is_drifted else CompareStatus.UNCHANGED

    return (
        FileCompare(
            name=name,
            status=status,
            diff=diff,
            expected_drift_keys=expected_keys,
            unexpected_drift_keys=unexpected_keys,
        ),
        bool(diff),
    )


def compare_summary_table(report: CompareReport) -> Table:
    """Build a rich :class:`~rich.table.Table` summarising the compare report.

    One row per ``DRIFTED`` entry with columns: ``file``, ``expected drift``,
    ``unexpected drift``. Expected-drift counts render in dim cyan; unexpected
    in bold red when > 0.
    """
    table = Table(title="Drift Summary", show_header=True, header_style="bold")
    table.add_column("file")
    table.add_column("expected drift", justify="right")
    table.add_column("unexpected drift", justify="right")

    for entry in report.entries:
        if entry.status != CompareStatus.DRIFTED:
            continue
        exp_count = len(entry.expected_drift_keys)
        unexp_count = len(entry.unexpected_drift_keys)
        exp_str = f"[dim cyan]{exp_count}[/dim cyan]"
        if unexp_count > 0:
            unexp_str = f"[bold red]{unexp_count}[/bold red]"
        else:
            unexp_str = str(unexp_count)
        table.add_row(entry.name, exp_str, unexp_str)

    return table
