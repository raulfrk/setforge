"""Unit tests for the docs-sync drift check (scripts/docs_sync.py).

The check is ADVISORY: it never mutates files and never blocks; it emits a
proposal into the self-improvement ledger for each drift it finds and exits 1
when any drift is present, 0 when the doc set is clean, 2 on an infra error
(missing/unreadable RULES.md).

Each test points the check at a doc set built under ``tmp_path`` (or the
committed clean fixture) and asserts on the structured ``DriftFinding`` list
returned by :func:`scripts.docs_sync.find_drift`, plus the exit code from
:func:`scripts.docs_sync.run`. The ledger is redirected to a tmp file via the
``SETFORGE_PROPOSALS_LEDGER`` env var so emitted proposals do not touch the
real ledger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import docs_sync

FIXTURE = Path(__file__).parent / "fixtures" / "docs_sync_clean"


def _docset(
    root: Path,
    *,
    rules: str,
    changelog: str = "## [Unreleased]\n",
    agents: dict[str, str] | None = None,
    skills: dict[str, str] | None = None,
) -> docs_sync.DocSet:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "RULES.md").write_text(rules, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    agent_dir = root / "agents"
    skill_dir = root / "skills"
    agent_dir.mkdir(exist_ok=True)
    skill_dir.mkdir(exist_ok=True)
    for name, body in (agents or {}).items():
        (agent_dir / name).write_text(body, encoding="utf-8")
    for name, body in (skills or {}).items():
        (skill_dir / name).write_text(body, encoding="utf-8")
    return docs_sync.DocSet(
        rules=root / "docs" / "RULES.md",
        changelog=root / "CHANGELOG.md",
        enforcer_dirs=(agent_dir, skill_dir),
    )


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    led = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("SETFORGE_PROPOSALS_LEDGER", str(led))
    return led


def _ledger_keys(led: Path) -> list[str]:
    if not led.exists():
        return []
    return [json.loads(ln)["key"] for ln in led.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# Structural parsing
# --------------------------------------------------------------------------- #
def test_parse_rule_rows_anchored_to_table_cells() -> None:
    rules = (
        "# rules\n\n"
        "| ID | Statement | Tag | Enforced by |\n"
        "|---|---|---|---|\n"
        "| XY-1 | first | DETERMINISTIC | `foo` lint |\n"
        "| XY-2 | second | ADVISORY | `bar` agent |\n"
        "\n"
        "Prose mentioning XY-3 in a sentence is NOT a rule row.\n"
    )
    rows = docs_sync.parse_rule_rows(rules)
    assert {r.rule_id for r in rows} == {"XY-1", "XY-2"}


def test_enforced_by_planned_detected_structurally() -> None:
    rows = docs_sync.parse_rule_rows(
        "| ID | Statement | Tag | Enforced by |\n"
        "|---|---|---|---|\n"
        "| XY-1 | x | DETERMINISTIC | the lint — *planned; lands later* |\n"
    )
    assert rows[0].planned is True


# --------------------------------------------------------------------------- #
# Clean fixture
# --------------------------------------------------------------------------- #
def test_clean_fixture_has_zero_drift_and_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    docset = docs_sync.DocSet(
        rules=FIXTURE / "docs" / "RULES.md",
        changelog=FIXTURE / "CHANGELOG.md",
        enforcer_dirs=(FIXTURE / "agents", FIXTURE / "skills"),
    )
    assert docs_sync.find_drift(docset) == []
    assert docs_sync.run(docset) == 0


# --------------------------------------------------------------------------- #
# Drift pattern (a): planned enforcer, but a real enforcer cites the rule.
# --------------------------------------------------------------------------- #
def test_drift_planned_but_enforcer_exists(
    tmp_path: Path, isolated_ledger: Path
) -> None:
    docset = _docset(
        tmp_path,
        rules=(
            "| ID | Statement | Tag | Enforced by |\n"
            "|---|---|---|---|\n"
            "| XY-1 | x | DETERMINISTIC | the lint — *planned* |\n"
        ),
        changelog="## [Unreleased]\n- Shipped XY-1.\n",
        agents={"r.md": "This agent enforces XY-1 now."},
    )
    findings = docs_sync.find_drift(docset)
    kinds = {(f.kind, f.rule_id) for f in findings}
    assert ("planned-but-enforced", "XY-1") in kinds
    # exit 1 + a proposal was emitted (recorded as a "seen" row).
    assert docs_sync.run(docset) == 1
    assert _ledger_keys(isolated_ledger)


# --------------------------------------------------------------------------- #
# Drift pattern (b): rule cited by an enforcer but absent from the index.
# --------------------------------------------------------------------------- #
def test_drift_cited_but_not_indexed(tmp_path: Path) -> None:
    docset = _docset(
        tmp_path,
        rules=(
            "| ID | Statement | Tag | Enforced by |\n"
            "|---|---|---|---|\n"
            "| XY-1 | x | DETERMINISTIC | `foo` lint |\n"
        ),
        changelog="## [Unreleased]\n- Shipped XY-1.\n",
        agents={"r.md": "This agent enforces XY-1 and also XY-9 (unindexed)."},
    )
    findings = docs_sync.find_drift(docset)
    assert ("cited-not-indexed", "XY-9") in {(f.kind, f.rule_id) for f in findings}


def test_substring_rule_id_does_not_false_match(tmp_path: Path) -> None:
    # XY-1 must not be considered "cited" by the token XY-12 (full-token only).
    docset = _docset(
        tmp_path,
        rules=(
            "| ID | Statement | Tag | Enforced by |\n"
            "|---|---|---|---|\n"
            "| XY-1 | x | DETERMINISTIC | `foo` lint |\n"
        ),
        changelog="## [Unreleased]\n- Shipped XY-1.\n",
        agents={"r.md": "Mentions XY-12 only."},
    )
    findings = docs_sync.find_drift(docset)
    # XY-1 should be flagged dead (no real enforcer cites it), and XY-12 should
    # be the cited-not-indexed one — proving XY-12 did not satisfy XY-1.
    by_kind = {(f.kind, f.rule_id) for f in findings}
    assert ("dead-rule", "XY-1") in by_kind
    assert ("cited-not-indexed", "XY-12") in by_kind


# --------------------------------------------------------------------------- #
# Drift pattern (c): indexed rule no enforcer cites (dead rule).
# --------------------------------------------------------------------------- #
def test_drift_dead_rule(tmp_path: Path) -> None:
    docset = _docset(
        tmp_path,
        rules=(
            "| ID | Statement | Tag | Enforced by |\n"
            "|---|---|---|---|\n"
            "| XY-1 | x | DETERMINISTIC | `foo` lint |\n"
        ),
        changelog="## [Unreleased]\n- Shipped XY-1.\n",
        agents={"r.md": "Enforces nothing relevant."},
    )
    findings = docs_sync.find_drift(docset)
    assert ("dead-rule", "XY-1") in {(f.kind, f.rule_id) for f in findings}


# --------------------------------------------------------------------------- #
# Drift pattern (d): shipped rule missing from the changelog.
# --------------------------------------------------------------------------- #
def test_drift_shipped_rule_missing_from_changelog(tmp_path: Path) -> None:
    docset = _docset(
        tmp_path,
        rules=(
            "| ID | Statement | Tag | Enforced by |\n"
            "|---|---|---|---|\n"
            "| XY-1 | x | DETERMINISTIC | `foo` lint |\n"
        ),
        changelog="## [Unreleased]\n- Nothing about this rule.\n",
        agents={"r.md": "Enforces XY-1 for real."},
    )
    findings = docs_sync.find_drift(docset)
    assert ("missing-from-changelog", "XY-1") in {(f.kind, f.rule_id) for f in findings}


# --------------------------------------------------------------------------- #
# Infra: missing RULES.md → exit 2, never a false clean.
# --------------------------------------------------------------------------- #
def test_missing_rules_file_is_infra_exit_2(tmp_path: Path) -> None:
    docset = docs_sync.DocSet(
        rules=tmp_path / "does-not-exist.md",
        changelog=tmp_path / "CHANGELOG.md",
        enforcer_dirs=(),
    )
    assert docs_sync.run(docset) == 2
