"""Record host-local additive sections INTO the reconcile store as LOCAL units.

The write-in counterpart to :mod:`setforge.reconcile.host_local_view` (which
reads host-local sections back OUT). Two call sites inject additive markdown
sections at end-of-file and then persist them as LOCAL units carrying a stable
``reloc_anchor`` heading identity:

- the install-time template seed (:func:`seed_section_slots_to_store`, housed
  here), and
- the 3.0 -> 4.0 span-surface-retire migration fold
  (``setforge.migrations._span_surface_retire._fold_sections``).

Both share the SAME two operations — deriving a body's heading identity and the
re-classify/mark-LOCAL/serialize/record dance — which MUST agree byte-for-byte
with the ``reloc_anchor`` :func:`setforge.reconcile.hunks.serialize` mints, so
the seed-once gate, the fold-idempotency gate, and the projection back out all
line up. Housing both here keeps that guarantee in one place instead of two
verbatim copies that can silently diverge.
"""

from __future__ import annotations

import stat
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from setforge.errors import ConfigError
from setforge.reconcile import store
from setforge.reconcile.hunks import (
    _section_heading,
    classify,
    extract_hunks,
    serialize,
)
from setforge.reconcile.types import FileId, HunkClass

if TYPE_CHECKING:
    from setforge.config import Config, ResolvedProfile, SectionTemplateRef

__all__ = [
    "record_local_reloc_sections",
    "resolve_template_src",
    "section_heading_of_body",
    "seed_section_slots_to_store",
]


def section_heading_of_body(body: bytes) -> str | None:
    """The heading identity a canonical section body will mint, or ``None``.

    Runs the SAME derivation the store uses when it persists a LOCAL host-local
    unit — :func:`setforge.reconcile.hunks._section_heading` over the hunk
    :func:`~setforge.reconcile.hunks.extract_hunks` produces for a pure insert of
    the body against an empty base — so the seed-once gate, the fold-idempotency
    comparison, and the LOCAL marking all agree byte-for-byte with the
    ``reloc_anchor`` :func:`~setforge.reconcile.hunks.serialize` mints. Reads the
    body's OWN first heading only (empty base ⇒ no neighbour heading to borrow),
    so a headingless body returns ``None`` and is refused up front rather than
    silently minting a neighbour's heading.
    """
    hunks = extract_hunks(b"", body)
    return _section_heading(hunks[0]) if hunks else None


def record_local_reloc_sections(
    profile: str,
    fid: FileId,
    *,
    base: bytes,
    new_local: bytes,
    existing_hunks: list[dict[str, object]],
    residual_headings: set[str],
) -> None:
    """Re-classify ``base -> new_local`` and record the injected sections LOCAL.

    Call inside the profile lock, AFTER ``new_local`` has the residual section
    bodies injected. Re-extracts the ``base -> new_local`` hunks and
    :func:`~setforge.reconcile.hunks.classify` them against ``existing_hunks`` so
    every prior classification (SHARED / LOCAL / SHARED_DRAFTED, INV-8 stage
    fidelity) is carried forward untouched; marks ONLY the freshly-injected
    section hunks LOCAL — a still-PENDING hunk whose own heading is one of
    ``residual_headings`` — so :func:`~setforge.reconcile.hunks.serialize` mints
    their ``reloc_anchor``; then :func:`~setforge.reconcile.store.record` s the
    merged base+local+hunks (drafts preserved: ``record`` leaves the on-disk
    drafts manifest intact when passed no ``drafts=``).
    """
    classified = classify(extract_hunks(base, new_local), existing_hunks)
    rows = serialize(
        [
            replace(hunk, cls=HunkClass.LOCAL)
            if hunk.cls is HunkClass.PENDING
            and _section_heading(hunk) in residual_headings
            else hunk
            for hunk in classified
        ]
    )
    store.record(profile, fid, base=base, local=new_local, hunks=rows)


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
    and records it as a LOCAL unit via :func:`record_local_reloc_sections`
    (host-local, survives re-baselining). Returns the section names newly seeded
    (empty on the
    seed-once no-op / no slots / no markdown target).

    Writes NOTHING to ``local.yaml``; the host-local intent lives only
    in the reconcile store, where :func:`host_local_sections_from_store` projects
    it back for the seed-once gate and every other consumer. Raises
    :class:`~setforge.errors.ConfigError` on an unreadable or headingless
    template body (the store identity is heading-based, so a headingless body has
    no stable ``reloc_anchor`` to fold onto).
    """
    from setforge import atomicio, reconcile
    from setforge.anchors import AnchorAtEndOfFile
    from setforge.body_canon import canonical_body, inject_body_at_anchor
    from setforge.compare import resolve_dst, resolve_src
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
    mode = stat.S_IMODE(dst.stat().st_mode) if dst.exists() else tracked_file.mode
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
