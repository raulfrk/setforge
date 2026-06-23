"""F5 wiring acceptance: the enforce-tests skill and the CLAUDE.md manifest each
name the full review roster (3 gate scripts + 2 project agents + the 2 global
fans), pin the canonical base ref, and the manifest declares bd-leak ownership;
the skill carries the pending-F2b seam line. Gate-step acceptance (both
directions) leans on F2a's pure ``check_source`` so it has no repo-state
dependence.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.check_policy_lints import check_source

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / ".claude/skills/enforce-tests/SKILL.md"
CLAUDE = REPO / "CLAUDE.md"

# The roster the skill + manifest must both name.
GATE_SCRIPTS = ["check_policy_lints.py", "check_schema_gates.py", "check-no-bd-refs.sh"]
AGENTS = ["test-quality-reviewer", "design-invariant-reviewer"]
GLOBAL_FANS = ["reviewing-python-code", "reviewing-bd-leaks"]
BASE_REF = "merge-base HEAD origin/main"


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must open with YAML frontmatter"
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def test_skill_file_exists_with_frontmatter() -> None:
    assert SKILL.exists(), "enforce-tests SKILL.md must exist"
    fm = _frontmatter(SKILL.read_text())
    assert fm.get("name") == "enforce-tests"
    assert fm.get("description"), "frontmatter needs a description"


def test_skill_names_full_roster() -> None:
    body = SKILL.read_text()
    for tok in GATE_SCRIPTS + AGENTS + GLOBAL_FANS:
        assert tok in body, f"skill must name {tok}"
    assert BASE_REF in body, "skill must pin the canonical base ref"
    assert "PENDING F2b" in body, "skill must carry the pending-F2b seam line"


def test_manifest_section_present_and_complete() -> None:
    text = CLAUDE.read_text()
    assert "enforce-tests" in text, "CLAUDE.md must declare the enforce-tests manifest"
    for tok in GATE_SCRIPTS + AGENTS + GLOBAL_FANS:
        assert tok in text, f"manifest must name {tok}"
    assert BASE_REF in text, "manifest must pin the canonical base ref"
    assert re.search(r"owns the[^\n]{0,20}reviewing-bd-leaks", text), (
        "manifest must declare bd-leak-scan ownership, not just mention it"
    )


def test_documented_gate_scripts_exist() -> None:
    for s in GATE_SCRIPTS:
        assert (REPO / "scripts" / s).exists(), f"documented gate script missing: {s}"


def test_gate_distinguishes_clean_from_violation() -> None:
    # F5 acceptance #1, both directions, via F2a's pure function.
    clean = check_source("x = 1\n", "setforge/new.py")
    assert clean == [], "clean source must yield no violations"
    dirty = check_source(
        "import subprocess\nsubprocess.run(c, shell=True)\n", "setforge/new.py"
    )
    assert len(dirty) >= 1, "shell=True must be flagged (gate exit-1 direction)"
