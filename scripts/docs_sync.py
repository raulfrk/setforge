#!/usr/bin/env python3
"""Deterministic docs-sync drift check (ADVISORY, never mutates, never blocks).

A STANDALONE script (NOT a pytest test — pytest is skippable via markers and
``addopts``, which would silently disarm the contract; same reasoning as
:mod:`scripts.check_policy_lints` / :mod:`scripts.check_schema_gates`). It
cross-checks the rule index in ``docs/RULES.md`` against the concrete
enforcers (the review agents + skills under ``.claude/``) and the changelog,
and surfaces four classes of documentation drift as proposals into the
self-improvement loop (:mod:`scripts.proposals`):

(a) **planned-but-enforced** — a rule whose "Enforced by" cell still says
    *planned*, yet a real enforcer (an agent / skill) already cites its id.
(b) **cited-not-indexed** — a rule id cited by an enforcer that is absent from
    the RULES.md index (a stale or typo'd citation, or a rule that shipped in
    code but never made the index).
(c) **dead-rule** — a rule in the index that no enforcer cites and whose
    "Enforced by" cell names no concrete mechanism (orphaned rule).
(d) **missing-from-changelog** — a shipped rule (real enforcer, not planned)
    whose id never appears in the changelog.

Parsing is STRUCTURAL, never positional: rule rows are read from the markdown
table cells (a leading ``| ID | ...`` cell that is a whole rule-id token), and
citations are matched as whole tokens (``\\bAB-1\\b``) so ``AB-12`` never
satisfies ``AB-1`` and a prose mention of an id outside a table cell is not
mistaken for a rule row. No ``rg -A1`` / ``awk`` line-windows, no case-folding
substitutions — those manufacture false drift.

Exit ``0`` clean / ``1`` drift found / ``2`` INFRA error (RULES.md missing or
unreadable). Each drift emits one grounded proposal; emission is advisory and
never affects the exit code beyond the drift it records.

Invocation::

    uv run python scripts/docs_sync.py
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.proposals import Confidence, Proposal, emit

REPO_ROOT = Path(__file__).resolve().parent.parent

# A rule id is a stable prefix + dash + digits (INV-1, SAFE-10, …). Whole-token
# only, so AB-12 never matches an AB-1 search and prose punctuation does not
# bleed into the id.
_RULE_ID = re.compile(r"\b([A-Z]{2,5}-\d+)\b")
# A markdown table row whose FIRST cell is exactly a rule id → a real rule row.
# Anchored on the cell boundary (``| ID |``), not a line-window.
_TABLE_ROW = re.compile(r"^\s*\|\s*([A-Z]{2,5}-\d+)\s*\|(.*)\|\s*$")
# The "planned" marker the index uses in an "Enforced by" cell.
_PLANNED = re.compile(r"\bplanned\b", re.IGNORECASE)
# A concrete deterministic enforcer named in the "Enforced by" cell: a
# ``scripts/...py`` gate path. Used to decide whether a rule with no external
# (agent/skill) citation is genuinely dead (c) vs. enforced by a deterministic
# script gate that does not "cite" the id in enforcer-prose. Prose words like
# "lint" / "agent" alone do NOT count — they are exactly the stale-claim
# surface drift is meant to catch; only a real file path anchors enforcement.
_MECHANISM = re.compile(r"scripts/\S+\.py")


@dataclass(frozen=True, slots=True)
class RuleRow:
    """One parsed RULES.md rule row: its id, whether the enforcer is still
    *planned*, and whether the "Enforced by" cell names a concrete mechanism."""

    rule_id: str
    planned: bool
    has_mechanism: bool


@dataclass(frozen=True, slots=True)
class DocSet:
    """The files a drift run reads. ``enforcer_dirs`` are scanned recursively
    for ``*.md`` enforcer definitions (the review agents + skills)."""

    rules: Path
    changelog: Path
    enforcer_dirs: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class DriftFinding:
    """One drift: its kind (one of the four patterns), the rule id, and a
    human-readable evidence line citing the external signal."""

    kind: str
    rule_id: str
    evidence: str


class InfraError(Exception):
    """A precondition failed (RULES.md missing/unreadable) — exit 2, never a
    false clean."""


def parse_rule_rows(text: str) -> list[RuleRow]:
    """Parse the RULES.md rule index from its markdown table cells (structural).

    Only a line whose first table cell is a whole rule-id token counts; the
    "Enforced by" cell is the last cell on that row.
    """
    rows: list[RuleRow] = []
    for line in text.splitlines():
        m = _TABLE_ROW.match(line)
        if not m:
            continue
        rule_id = m.group(1)
        cells = [c.strip() for c in m.group(2).split("|")]
        enforced_by = cells[-1] if cells else ""
        rows.append(
            RuleRow(
                rule_id=rule_id,
                planned=bool(_PLANNED.search(enforced_by)),
                has_mechanism=bool(_MECHANISM.search(enforced_by)),
            )
        )
    return rows


def _cited_ids(enforcer_dirs: tuple[Path, ...]) -> set[str]:
    """Whole-token rule ids cited across every ``*.md`` under the enforcer dirs."""
    cited: set[str] = set()
    for directory in enforcer_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.md")):
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                # An unreadable enforcer file is a soft miss, not an infra error:
                # the index + changelog are the load-bearing inputs. Skip it.
                continue
            cited.update(_RULE_ID.findall(body))
    return cited


def find_drift(docset: DocSet) -> list[DriftFinding]:
    """Return every drift finding across the four patterns. Raises
    :class:`InfraError` if RULES.md cannot be read."""
    try:
        rules_text = docset.rules.read_text(encoding="utf-8")
    except OSError as exc:
        raise InfraError(f"cannot read rule index {docset.rules}: {exc}") from exc
    try:
        changelog_text = docset.changelog.read_text(encoding="utf-8")
    except OSError:
        changelog_text = ""

    rows = parse_rule_rows(rules_text)
    indexed: dict[str, RuleRow] = {r.rule_id: r for r in rows}
    cited = _cited_ids(docset.enforcer_dirs)
    changelog_ids = set(_RULE_ID.findall(changelog_text))

    findings: list[DriftFinding] = []

    # (a) planned-but-enforced + (c) dead-rule + (d) missing-from-changelog
    for rule_id, row in indexed.items():
        is_cited = rule_id in cited
        # A rule is "live" if a real enforcer cites it OR its cell names a
        # concrete (non-planned) mechanism.
        is_enforced = is_cited or (row.has_mechanism and not row.planned)

        if row.planned and is_cited:
            findings.append(
                DriftFinding(
                    "planned-but-enforced",
                    rule_id,
                    f"RULES.md marks {rule_id} 'planned' but an enforcer under "
                    f"{', '.join(str(d) for d in docset.enforcer_dirs)} cites it.",
                )
            )
        elif not is_enforced:
            findings.append(
                DriftFinding(
                    "dead-rule",
                    rule_id,
                    f"RULES.md rule {rule_id} is cited by no enforcer and names "
                    f"no concrete mechanism in its 'Enforced by' cell.",
                )
            )

        # (d) only for rules that have actually shipped (real enforcer, not
        # planned) — a planned rule is legitimately not in the changelog yet.
        if is_enforced and not row.planned and rule_id not in changelog_ids:
            findings.append(
                DriftFinding(
                    "missing-from-changelog",
                    rule_id,
                    f"Shipped rule {rule_id} (enforced, not planned) does not "
                    f"appear in {docset.changelog.name}.",
                )
            )

    # (b) cited-not-indexed
    for rule_id in sorted(cited - indexed.keys()):
        findings.append(
            DriftFinding(
                "cited-not-indexed",
                rule_id,
                f"Rule {rule_id} is cited by an enforcer but is absent from the "
                f"RULES.md index.",
            )
        )

    return findings


def _emit_finding(finding: DriftFinding, docset: DocSet) -> None:
    """File one drift as a grounded, advisory proposal (proposed_diff empty —
    the human resolves doc drift by hand)."""
    emit(
        Proposal(
            source="docs-sync",
            category="docs-drift",
            evidence=f"[{finding.kind}] {finding.evidence}",
            proposed_diff="",
            confidence=Confidence.MEDIUM,
            file=str(docset.rules),
        )
    )


def run(docset: DocSet) -> int:
    """Run the drift check over ``docset``. Returns ``0`` clean / ``1`` drift /
    ``2`` infra error."""
    try:
        findings = find_drift(docset)
    except InfraError as exc:
        print(f"docs-sync: COULD NOT RUN — {exc}", file=sys.stderr)
        return 2

    if not findings:
        return 0

    print(
        "docs-sync: documentation drift (advisory — see docs/RULES.md):",
        file=sys.stderr,
    )
    for f in sorted(findings, key=lambda f: (f.kind, f.rule_id)):
        print(f"  {f.kind} {f.rule_id}: {f.evidence}", file=sys.stderr)
        _emit_finding(f, docset)
    return 1


def _default_docset() -> DocSet:
    return DocSet(
        rules=REPO_ROOT / "docs" / "RULES.md",
        changelog=REPO_ROOT / "CHANGELOG.md",
        enforcer_dirs=(
            REPO_ROOT / ".claude" / "agents",
            REPO_ROOT / ".claude" / "skills",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="override the repo root (defaults to this script's parent's parent)",
    )
    # Programmatic callers default to an EMPTY argv (never sys.argv); only
    # __main__ forwards the real command line.
    args = parser.parse_args([] if argv is None else argv)
    if args.repo_root is None:
        docset = _default_docset()
    else:
        root = args.repo_root.resolve()
        docset = DocSet(
            rules=root / "docs" / "RULES.md",
            changelog=root / "CHANGELOG.md",
            enforcer_dirs=(root / ".claude" / "agents", root / ".claude" / "skills"),
        )
    return run(docset)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
