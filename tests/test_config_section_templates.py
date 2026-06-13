"""Schema + resolve-inheritance + cross-ref tests for section-template slots.

Covers the additive schema surface of the SEED-ONCE host-local section
template library: the top-level ``section_templates:`` registry
(:class:`SectionTemplateRef`), the per-profile ``section_slots:`` map, its
dict-merge inheritance through ``extends:``, and the load-time cross-ref
validation that every slot value names a declared template.
"""

from pathlib import Path

import pytest

from setforge.config import (
    Config,
    Profile,
    ResolvedProfile,
    SectionTemplateRef,
    TrackedFile,
    load_config,
    resolve_profile,
)
from setforge.errors import ConfigError


def _cfg(
    profiles: dict[str, Profile],
    *,
    section_templates: dict[str, SectionTemplateRef] | None = None,
) -> Config:
    return Config(
        tracked_files={"d": TrackedFile(src=Path("a"), dst="b")},
        section_templates=section_templates or {},
        profiles=profiles,
    )


def test_section_template_ref_src() -> None:
    ref = SectionTemplateRef(src=Path("python-conventions.md"))
    assert ref.src == Path("python-conventions.md")


def test_config_defaults_empty_section_templates() -> None:
    cfg = _cfg({"only": Profile()})
    assert cfg.section_templates == {}


def test_profile_defaults_empty_section_slots() -> None:
    assert Profile().section_slots == {}
    assert ResolvedProfile().section_slots == {}


def test_load_config_parses_section_templates_and_slots(tmp_path: Path) -> None:
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(
        """\
version: 1
tracked_files:
  claude_md:
    src: claude/CLAUDE.md
    dst: ~/.claude/CLAUDE.md
section_templates:
  python-conv:
    src: python-conventions.md
profiles:
  base:
    tracked_files: [claude_md]
    section_slots:
      Python conventions: python-conv
"""
    )
    cfg = load_config(config_path)
    assert cfg.section_templates["python-conv"].src == Path("python-conventions.md")
    assert cfg.profiles["base"].section_slots == {"Python conventions": "python-conv"}


def test_resolve_section_slots_dict_merge_child_overrides(tmp_path: Path) -> None:
    cfg = _cfg(
        {
            "parent": Profile(section_slots={"A": "t1", "B": "t2"}),
            "child": Profile(extends="parent", section_slots={"B": "t3", "C": "t4"}),
        },
        section_templates={
            "t1": SectionTemplateRef(src=Path("t1.md")),
            "t2": SectionTemplateRef(src=Path("t2.md")),
            "t3": SectionTemplateRef(src=Path("t3.md")),
            "t4": SectionTemplateRef(src=Path("t4.md")),
        },
    )
    resolved = resolve_profile(cfg, "child")
    # Parent-only key kept; shared key overridden by child; child-only added.
    assert resolved.section_slots == {"A": "t1", "B": "t3", "C": "t4"}


def test_resolve_section_slots_inherits_when_child_unset() -> None:
    cfg = _cfg(
        {
            "parent": Profile(section_slots={"A": "t1"}),
            "child": Profile(extends="parent"),
        },
        section_templates={"t1": SectionTemplateRef(src=Path("t1.md"))},
    )
    resolved = resolve_profile(cfg, "child")
    assert resolved.section_slots == {"A": "t1"}


def test_load_config_rejects_unknown_template_name(tmp_path: Path) -> None:
    """A slot value naming a template missing from the registry raises
    ConfigError naming the profile, the slot, and the offending template."""
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(
        """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
section_templates:
  declared-template:
    src: declared.md
profiles:
  base:
    tracked_files: [d]
    section_slots:
      Some section: missing-template
"""
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)
    msg = str(exc_info.value)
    assert "missing-template" in msg
    assert "base" in msg
    assert "Some section" in msg


def test_load_config_collects_multiple_unknown_template_names(tmp_path: Path) -> None:
    config_path = tmp_path / "setforge.yaml"
    config_path.write_text(
        """\
version: 1
tracked_files:
  d:
    src: x
    dst: y
profiles:
  alpha:
    tracked_files: [d]
    section_slots:
      S1: ghost-a
  beta:
    tracked_files: [d]
    section_slots:
      S2: ghost-b
"""
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)
    msg = str(exc_info.value)
    assert "ghost-a" in msg
    assert "ghost-b" in msg
