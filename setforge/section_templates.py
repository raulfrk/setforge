"""Seed-once host-local section templates from the config-repo library.

A SEED-ONCE template library, distinct from the disposition (pinned /
forked) model and from the shared-section three-way reconciler. The
top-level ``Config.section_templates`` registry maps a template NAME →
:class:`~setforge.config.SectionTemplateRef` (a body file under the
config-repo's ``templates/`` directory). A profile's
``section_slots`` maps a host-local user-section NAME → a template name.

On install, AFTER deploy and UNDER the profile lock, a section named in
``section_slots`` whose heading is not yet a host-local unit in the
reconcile store is seeded ONCE: the template body is injected at
end-of-file into the target markdown tracked file's stored
``base``/``local`` and recorded as a LOCAL ``reloc_anchor`` unit
(:func:`seed_section_slots_to_store`). The reconcile engine then owns
that unit's deploy + drift natively, so the seeded content rides the
standard host-local survival path:

- A section whose heading is ALREADY a LOCAL store unit (the host has
  adopted / edited it) is NEVER reseeded — the store gate skips it.
- Template-body edits in the library do NOT propagate: seeding fires
  only when the heading is absent from the store, so a populated section
  is left untouched on every subsequent install.

STAGE B retired the ``local.yaml`` ``host_local_sections`` /
overlay-``spans`` surface; seeding writes NOTHING to ``local.yaml`` — the
host-local intent lives ONLY in the reconcile per-unit store.
"""

from __future__ import annotations

from pathlib import Path

from setforge.config import Config, ResolvedProfile, SectionTemplateRef
from setforge.errors import ConfigError

__all__ = [
    "resolve_template_src",
    "seed_section_slots_to_store",
]


def resolve_template_src(ref: SectionTemplateRef, repo_root: Path) -> Path:
    """Resolve a template ``src`` (relative to ``templates/``) to an absolute path.

    Mirrors :func:`setforge.compare.resolve_src` (which roots tracked-file
    ``src`` at ``<repo>/tracked/``); the template library is rooted at
    ``<repo>/templates/`` instead.
    """
    return repo_root / "templates" / ref.src


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


def seed_section_slots_to_store(
    cfg: Config,
    resolved: ResolvedProfile,
    repo_root: Path,
    profile: str,
) -> list[str]:
    """Seed a profile's ``section_slots`` as LOCAL reconcile-store units (seed-once).

    Call INSIDE ``profile_lock`` and AFTER deploy (so deploy's pre-install store
    snapshot is the pre-seed baseline a ``revert`` restores to). For each slot
    whose template-body heading is not already a LOCAL+``reloc_anchor`` unit in
    the profile's reconcile store, inject the canonical template body at
    end-of-file into the target markdown tracked file's stored ``base``/``local``
    and :func:`~setforge.reconcile.record` it as a LOCAL unit (host-local,
    survives re-baselining). Returns the section names newly seeded (empty on the
    seed-once no-op / no slots / no markdown target).

    Writes NOTHING to ``local.yaml``; the host-local intent lives only
    in the reconcile store, where :func:`host_local_sections_from_store` projects
    it back for the seed-once gate and every other consumer. Raises
    :class:`~setforge.errors.ConfigError` on an unreadable or headingless
    template body (the store identity is heading-based, so a headingless body has
    no stable ``reloc_anchor`` to fold onto).
    """
    import stat as stat_mod

    from setforge import atomicio, reconcile
    from setforge.anchors import AnchorAtEndOfFile
    from setforge.compare import resolve_dst, resolve_src
    from setforge.overlay_inject import canonical_body, inject_body_at_anchor
    from setforge.reconcile.host_local_view import host_local_sections_from_store
    from setforge.reconcile.types import file_id

    if not resolved.section_slots:
        return []
    target_id = _first_markdown_tracked_file(cfg, resolved)
    if target_id is None:
        return []
    fid = file_id(target_id)

    # Seed-once: a heading already a LOCAL store unit is host-owned, skip it.
    proj = host_local_sections_from_store(profile, fid)
    already = set(proj.get(str(fid), {}))

    residual: list[tuple[str, str]] = []
    seeded: list[str] = []
    for section_name, template_name in resolved.section_slots.items():
        ref = cfg.section_templates[template_name]
        src = resolve_template_src(ref, repo_root)
        try:
            body = src.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"section_slots template {template_name!r} body file not "
                f"readable: {src} ({exc})"
            ) from exc
        cbody = canonical_body(body)
        heading = reconcile.section_heading_of_body(cbody.encode("utf-8"))
        if heading is None:
            raise ConfigError(
                f"section_slots template {template_name!r} body has no markdown "
                f"heading; the reconcile-store identity is heading-based, so a "
                f"headingless template cannot seed a stable host-local unit: {src}"
            )
        if heading in already:
            continue
        residual.append((heading, cbody))
        seeded.append(section_name)
    if not residual:
        return []

    tracked_file = cfg.tracked_files[target_id]
    dst = resolve_dst(tracked_file)
    stored_base = reconcile.read_base(profile, fid)
    if stored_base is not None:
        base = stored_base
    else:
        src_path = resolve_src(tracked_file, repo_root)
        base = src_path.read_bytes() if src_path.exists() else b""
    # The engine preserves LIVE, never injects from the store: write live directly.
    live_now = dst.read_bytes() if dst.exists() else base

    entry = reconcile.read_index(profile).files.get(str(fid))
    existing_hunks = list(entry.hunks) if entry is not None else []

    anchor = AnchorAtEndOfFile()
    text = live_now.decode("utf-8")
    for _heading, cbody in residual:
        text = inject_body_at_anchor(text, anchor, cbody)
    new_live = text.encode("utf-8")

    # Preserve the current mode (deploy just set it): no spurious re-mode.
    mode = stat_mod.S_IMODE(dst.stat().st_mode) if dst.exists() else tracked_file.mode
    atomicio.atomic_write_bytes(dst, new_live, mode=mode)

    # Only the freshly-injected PENDING hunks get marked LOCAL (to mint reloc_anchor).
    residual_headings = {heading for heading, _ in residual}
    reconcile.record_local_reloc_sections(
        profile,
        fid,
        base=base,
        new_local=new_live,
        existing_hunks=existing_hunks,
        residual_headings=residual_headings,
    )
    return seeded
