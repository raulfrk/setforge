"""Orchestration for ``setforge section detect`` (S4/S5).

The pure detect engine lives in :mod:`setforge.section_detect`
(``compute_detect_regions`` / ``propose_anchor``); this module does the
config/IO/wizard plumbing the typer command needs:

* resolve each markdown tracked_file's tracked-``src`` + live-``dst`` under a
  profile,
* compute the **live-independent** expected-deploy string
  (:func:`expected_deploy_text`) so hand-edited regions surface as drift,
* run the carve wizard and write host-local spans to ``local.yaml`` atomically.

Kept separate from :mod:`setforge.cli.section` (already large) per the plan's
file-placement decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from setforge import deploy, spans_store
from setforge.compare import resolve_dst, resolve_src
from setforge.config import Config, TrackedFile, load_config, resolve_profile
from setforge.section_detect import compute_detect_regions
from setforge.source import HostLocalSection, HostLocalSectionName

_MARKDOWN_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown"})


@dataclass(slots=True, frozen=True)
class DetectTarget:
    """One markdown tracked_file resolved for a detect run."""

    name: str
    tracked_file: TrackedFile
    src: Path
    dst: Path


def _markdown_targets(
    cfg: Config, profile: str, repo_root: Path, tracked_file: str | None
) -> list[DetectTarget]:
    """Resolve the markdown detect targets for ``profile``.

    ``tracked_file`` (a tracked_files key) narrows to one entry; ``None`` walks
    every markdown tracked_file in the resolved profile. Non-markdown entries
    are skipped silently (detect is markdown-only).
    """
    resolved = resolve_profile(cfg, profile)
    names = [tracked_file] if tracked_file else list(resolved.tracked_files)
    out: list[DetectTarget] = []
    for name in names:
        if name not in resolved.tracked_files:
            raise KeyError(name)
        tf = cfg.tracked_files[name]
        src = resolve_src(tf, repo_root)
        dst = resolve_dst(tf)
        if src.suffix.lower() not in _MARKDOWN_SUFFIXES:
            continue
        out.append(DetectTarget(name=name, tracked_file=tf, src=src, dst=dst))
    return out


def _load_host_local_overlay(
    profile: str, file_id: str
) -> dict[HostLocalSectionName, HostLocalSection] | None:
    """Validated ``local.yaml`` ``host_local_sections`` overlay for ``file_id``.

    Stubbed for the command skeleton (Task 1); wired to the install/compare
    loader in Task 2.
    """
    return None


def _expected_base_text(profile: str, target: DetectTarget) -> str | None:
    """Disposition base (merge ancestor) for ``target``, or ``None``.

    Stubbed for the command skeleton (Task 1); wired to the install planner in
    Task 2.
    """
    return None


def expected_deploy_text(profile: str, target: DetectTarget) -> str:
    """Return the **live-independent** expected deploy output for ``target``.

    ``live_text=""`` is the load-bearing choice: it stops the disposition 3-way
    merge from absorbing the user's live hand-edits, so
    :func:`compute_detect_regions` surfaces them as drift. For the
    ``disposition=None`` markerless path the content is the tracked source with
    its overlay bodies injected — already live-independent.
    """
    tf = target.tracked_file
    file_spans = tf.spans or []
    states = spans_store.get_states(profile, target.name) if file_spans else {}
    host_local = _load_host_local_overlay(profile, target.name)
    base_text = _expected_base_text(profile, target)
    resolved = deploy.resolve_deploy(
        target.src,
        target.dst,
        host_local_sections=host_local,
        mode=tf.mode,
        disposition=tf.disposition,
        base_text=base_text,
        spans=file_spans or None,
        span_states=states or None,
        live_text="",
    )
    return resolved.content


def run_detect(*, config_path: Path, profile: str, tracked_file: str | None) -> None:
    """Top-level ``section detect`` entry point (skeleton — wizard lands in Task 5)."""
    cfg = load_config(config_path)
    repo_root = config_path.resolve().parent
    console = Console()
    targets = _markdown_targets(cfg, profile, repo_root, tracked_file)
    any_drift = False
    for target in targets:
        if not target.dst.exists():
            continue
        live = target.dst.read_text(encoding="utf-8")
        expected = expected_deploy_text(profile, target)
        regions = compute_detect_regions(live, expected)
        if not regions:
            continue
        any_drift = True
        console.print(
            f"{len(regions)} changed region(s) in {target.dst} "
            "(live vs expected deploy output)"
        )
    if not any_drift:
        console.print("no changes detected — live matches expected deploy output")
