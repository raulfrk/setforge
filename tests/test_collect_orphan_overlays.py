"""Unit tests for :func:`setforge.config.collect_orphan_overlays`.

The helper is the pure classifier that ``validate`` and ``compare`` both
call to surface ``local.yaml`` overlay entries the apply site silently
skips. Two orphan classes:

- ``unknown`` — the overlay id appears NOWHERE in ``cfg.tracked_files``
  (the full registry): a typo or a stale entry.
- ``off_profile`` — the id IS in ``cfg.tracked_files`` but not in THIS
  profile's resolved ``tracked_files`` list: a legitimate multi-profile
  host.

Companion to :mod:`tests.test_host_local_tracked_file_overrides`, which
locks down the apply site's mutation semantics (and asserts the apply
site stays silent on orphans).
"""

from __future__ import annotations

from pathlib import Path

from setforge.config import (
    Config,
    OrphanOverlay,
    OrphanOverlayClass,
    collect_orphan_overlays,
    load_config,
    resolve_profile,
)

# Two tracked_files in the registry; profile ``p`` includes only ``hook``.
# ``other`` is registry-known but off-profile for ``p``.
_BASE_YAML = (
    "version: 1\n"
    "tracked_files:\n"
    "  hook:\n"
    "    src: hook.sh\n"
    "    dst: ~/.tracked-host/hook.sh\n"
    "  other:\n"
    "    src: other.sh\n"
    "    dst: ~/.tracked-host/other.sh\n"
    "profiles:\n"
    "  p:\n"
    "    tracked_files:\n"
    "      - hook\n"
)


def _write_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "setforge.yaml"
    cfg.write_text(_BASE_YAML, encoding="utf-8")
    tracked = tmp_path / "tracked"
    tracked.mkdir(exist_ok=True)
    (tracked / "hook.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (tracked / "other.sh").write_text("#!/bin/sh\necho yo\n", encoding="utf-8")
    return cfg


def _load(tmp_path: Path) -> Config:
    return load_config(_write_cfg(tmp_path))


def test_no_local_yaml_returns_empty(tmp_path: Path) -> None:
    """Absent local.yaml ⇒ no orphans."""
    cfg = _load(tmp_path)
    resolved = resolve_profile(cfg, "p")
    assert collect_orphan_overlays(cfg, resolved) == []


def test_in_profile_overlay_is_not_an_orphan(tmp_path: Path) -> None:
    """An overlay on a tracked_file the profile DOES include is not an orphan."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  hook:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    resolved = resolve_profile(cfg, "p")
    assert collect_orphan_overlays(cfg, resolved) == []


def test_unknown_id_classified_unknown(tmp_path: Path) -> None:
    """An id absent from cfg.tracked_files ⇒ class ``unknown``."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  not_a_real_id:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    resolved = resolve_profile(cfg, "p")
    orphans = collect_orphan_overlays(cfg, resolved)
    assert orphans == [
        OrphanOverlay(id="not_a_real_id", class_=OrphanOverlayClass.UNKNOWN)
    ]


def test_off_profile_id_classified_off_profile(tmp_path: Path) -> None:
    """An id in cfg.tracked_files but not the profile's list ⇒ ``off_profile``."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  other:\n    mode: 0o755\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    resolved = resolve_profile(cfg, "p")
    orphans = collect_orphan_overlays(cfg, resolved)
    assert orphans == [OrphanOverlay(id="other", class_=OrphanOverlayClass.OFF_PROFILE)]


def test_empty_overlay_entry_is_not_an_orphan(tmp_path: Path) -> None:
    """An overlay entry that declares NO overlay fields is skipped — it
    never reaches the apply site, so it is not a surfaced orphan either
    (parity with the apply site's empty-overlay short-circuit)."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n  not_a_real_id: {}\n",
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    resolved = resolve_profile(cfg, "p")
    assert collect_orphan_overlays(cfg, resolved) == []


def test_both_classes_collected_together(tmp_path: Path) -> None:
    """Unknown + off-profile orphans surface together in one call."""
    (tmp_path / "local.yaml").write_text(
        "tracked_files:\n"
        "  hook:\n"
        "    mode: 0o755\n"  # in-profile — not an orphan
        "  other:\n"
        "    mode: 0o755\n"  # off-profile
        "  not_a_real_id:\n"
        "    mode: 0o755\n",  # unknown
        encoding="utf-8",
    )
    cfg = _load(tmp_path)
    resolved = resolve_profile(cfg, "p")
    orphans = collect_orphan_overlays(cfg, resolved)
    by_id = {o.id: o.class_ for o in orphans}
    assert by_id == {
        "other": OrphanOverlayClass.OFF_PROFILE,
        "not_a_real_id": OrphanOverlayClass.UNKNOWN,
    }
