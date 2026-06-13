"""Seed-once host-local section templates from the config-repo library.

A SEED-ONCE template library, distinct from the disposition (pinned /
forked) model and from the shared-section three-way reconciler. The
top-level ``Config.section_templates`` registry maps a template NAME →
:class:`~setforge.config.SectionTemplateRef` (a body file under the
config-repo's ``templates/`` directory). A profile's
``section_slots`` maps a host-local user-section NAME → a template name.

On install, before the deploy loop, an EMPTY or MISSING host-local
section named in ``section_slots`` is seeded ONCE: the template body is
written into the host's ``local.yaml`` as a ``host_local_sections``
overlay block keyed by the section NAME. The existing install pipeline
then migrates that block to a unified OVERLAY span and injects the body
at deploy time, so the seeded content rides the standard host-local
survival path:

- A section that ALREADY carries an overlay body (the host has adopted /
  edited it) is NEVER reseeded — the host owns it.
- Template-body edits in the library do NOT propagate: seeding fires
  only when the section is absent on the host, so a populated section is
  left untouched on every subsequent install.

This module never touches the shared-section reconciler
(:mod:`setforge.section_reconcile`); seeding is a pre-merge fill into the
host-owned overlay layer, not a drift reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from setforge.config import Config, ResolvedProfile, SectionTemplateRef
from setforge.errors import ConfigError
from setforge.migrations._yaml_ops import atomic_write_yaml, yaml_rt

__all__ = [
    "SeedPlanEntry",
    "plan_section_seeds",
    "resolve_template_src",
    "seed_section_templates",
]


def resolve_template_src(ref: SectionTemplateRef, repo_root: Path) -> Path:
    """Resolve a template ``src`` (relative to ``templates/``) to an absolute path.

    Mirrors :func:`setforge.compare.resolve_src` (which roots tracked-file
    ``src`` at ``<repo>/tracked/``); the template library is rooted at
    ``<repo>/templates/`` instead.
    """
    return repo_root / "templates" / ref.src


@dataclass(slots=True, frozen=True)
class SeedPlanEntry:
    """One planned seed: write ``body`` into ``section_name`` for ``tracked_file_id``.

    ``template_name`` is carried for diagnostics. The plan is computed
    against the host's CURRENT overlay state, so an entry appears ONLY for
    a section with no existing host-local body (the seed-once gate).
    """

    tracked_file_id: str
    section_name: str
    template_name: str
    body: str


def plan_section_seeds(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    *,
    existing_overlay: dict[str, set[str]],
) -> list[SeedPlanEntry]:
    """Compute the seed-once plan for a profile's ``section_slots``.

    For each slot ``(section_name → template_name)`` whose ``section_name``
    is NOT already present in the host's overlay for ANY tracked file, read
    the template body and emit a :class:`SeedPlanEntry` targeting the FIRST
    tracked markdown file in the resolved profile (insertion order). A slot
    whose section is already populated on the host yields no entry — the
    seed-once gate.

    The chosen tracked file is the profile's first markdown-suffixed entry;
    seeding has no per-file routing because a section NAME is the overlay
    IDENTITY (unique within a host), and the body is injected at
    end-of-file. Raises :class:`ConfigError` only on a genuinely
    unreadable template body (a missing file the validate gate did not
    catch), so install aborts cleanly before any write.
    """
    if not resolved.section_slots:
        return []
    already: set[str] = set()
    for names in existing_overlay.values():
        already |= names
    target_id = _first_markdown_tracked_file(cfg, resolved)
    if target_id is None:
        return []
    plan: list[SeedPlanEntry] = []
    for section_name, template_name in resolved.section_slots.items():
        if section_name in already:
            continue
        ref = cfg.section_templates[template_name]
        src = resolve_template_src(ref, repo_root)
        try:
            body = src.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"section_slots template {template_name!r} body file not "
                f"readable: {src} ({exc})"
            ) from exc
        plan.append(
            SeedPlanEntry(
                tracked_file_id=target_id,
                section_name=section_name,
                template_name=template_name,
                body=body,
            )
        )
    return plan


def _first_markdown_tracked_file(cfg: Config, resolved: ResolvedProfile) -> str | None:
    """Return the first resolved tracked_file id whose ``src`` is markdown.

    Host-local sections are supported only on markdown tracked_files (see
    :func:`setforge.source.validate_host_local_sections_file_type`), so the
    seed target must be one. ``None`` when the profile has no markdown
    tracked_file to host the seeded section.
    """
    for tf_id in resolved.tracked_files:
        tracked_file = cfg.tracked_files.get(tf_id)
        if tracked_file is None:
            continue
        if tracked_file.src.suffix.lower() in {".md", ".markdown"}:
            return tf_id
    return None


def _load_local_config_map(local_config_path: Path) -> CommentedMap:
    """Round-trip-load ``local.yaml`` as a :class:`CommentedMap`.

    A missing or empty file yields a fresh map. A non-mapping top level is
    a corrupt ``local.yaml``, so raise :class:`ConfigError` rather than
    clobber it with a fresh map (which would discard the user's content) —
    mirroring :mod:`setforge.source`'s non-mapping-top-level contract.
    """
    if not local_config_path.exists():
        return CommentedMap()
    with local_config_path.open("r", encoding="utf-8") as fh:
        data = yaml_rt().load(fh)
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        raise ConfigError(f"top-level of {local_config_path} must be a mapping")
    return data


def seed_section_templates(plan: list[SeedPlanEntry], local_config_path: Path) -> bool:
    """Write the seed plan into ``local.yaml`` host_local_sections (seed-once).

    Each :class:`SeedPlanEntry` becomes a
    ``tracked_files.<id>.host_local_sections.<section_name>`` block with an
    ``at-end-of-file`` anchor and the template ``body`` inline. The write
    is a ruamel round-trip via
    :func:`setforge.migrations._yaml_ops.atomic_write_yaml` (comments,
    order, quoting, and file mode preserved). Returns ``True`` when at
    least one section was written, ``False`` on a no-op — either the
    empty plan or a non-empty plan whose every section already carries a
    host-local body (nothing left to seed). No file write occurs in
    either no-op case, so re-running converges.

    A pre-existing ``host_local_sections.<name>`` for a planned section is
    left untouched — but ``plan_section_seeds`` already excludes populated
    sections, so this is a belt-and-suspenders guard against a concurrent
    edit between plan and write.
    """
    if not plan:
        return False
    data = _load_local_config_map(local_config_path)

    tracked_files = data.get("tracked_files")
    if tracked_files is None:
        tracked_files = CommentedMap()
        data["tracked_files"] = tracked_files
    elif not isinstance(tracked_files, CommentedMap):
        raise ConfigError(f"tracked_files in {local_config_path} must be a mapping")

    wrote = False
    for entry in plan:
        tf_block = tracked_files.get(entry.tracked_file_id)
        if not isinstance(tf_block, CommentedMap):
            tf_block = CommentedMap()
            tracked_files[entry.tracked_file_id] = tf_block
        sections = tf_block.get("host_local_sections")
        if not isinstance(sections, CommentedMap):
            sections = CommentedMap()
            tf_block["host_local_sections"] = sections
        if entry.section_name in sections:
            # Belt-and-suspenders: never overwrite an adopted section.
            continue
        section = CommentedMap()
        anchor = CommentedMap()
        anchor["kind"] = "at-end-of-file"
        section["anchor"] = anchor
        section["body"] = entry.body
        sections[entry.section_name] = section
        wrote = True

    if not wrote:
        return False
    atomic_write_yaml(local_config_path, data)
    return True
