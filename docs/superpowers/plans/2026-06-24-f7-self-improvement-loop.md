# F7 Self-Improvement Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the self-improvement loop spine — a typed `Proposal`, a durable append-only ledger that counts occurrences and remembers declines, and a `surface-proposals` skill that transitions filed proposals into the project task tracker and drives a safe approve/decline gate.

**Architecture:** One importable Python module `scripts/proposals.py` is the contract every producer calls via `emit()`. It is backed by a single append-only git-tracked JSONL **ledger** (`.claude/proposals/ledger.jsonl`) — the source of truth for occurrence-count and decline-suppress. The ledger is used instead of the task tracker for this state because the tracker's store is embedded single-writer and its compaction permanently removes closed items (which would race the counter and resurrect declined proposals). The committed Python never touches the task tracker. A markdown **skill** (in the hook-exempt `.claude/skills/` layer) reads the ledger's filed proposals, **transitions each into the task tracker** for the normal human workflow, and drives the safe `git apply` on approve.

**Tech Stack:** Python 3.12 (StrEnum, frozen dataclass, pathlib), `fcntl.flock` (mirroring `setforge/locking.py`), `subprocess` (argv-list, never shell), `git apply`, pytest + Hypothesis.

---

## File structure

| File | Responsibility |
|---|---|
| `scripts/proposals.py` | `Proposal` + `Confidence`, `norm()`/`dedup_key`, `Ledger`, `emit()`/`decline()`/`mark_applied()`/`list_filed()`, `validate_diff_paths()`/`approve()` |
| `.claude/proposals/ledger.jsonl` | append-only durable count + decline store (git-tracked; starts empty) |
| `.claude/proposals/.gitignore` | ignore the `*.lock` sibling |
| `.claude/skills/surface-proposals/SKILL.md` | the P6/session-end transition-to-tracker + approve/decline gate (exempt layer — names the tracker CLI) |
| `tests/test_proposals.py` | unit + property + concurrency + security tests |
| `docs/RULES.md` | flip SELF-1..4 "Enforced by" cells to `scripts/proposals.py` |

Reuse, don't reinvent: the `fcntl.flock(LOCK_EX)` idiom from `setforge/locking.py:profile_lock`; the standalone-script docstring/exit-code style from `scripts/check_policy_lints.py`; test import style `from scripts.proposals import ...` (see `tests/test_policy_lints.py`).

**Invisibility-rule note:** `scripts/` and `docs/` are scanned by `check-no-bd-refs`; `.claude/skills/` and `.claude/agents/` are exempt. Therefore all task-tracker mechanics (the concrete CLI commands) live ONLY in the `surface-proposals` skill. The Python module and this plan use the generic phrase "task tracker" and never name the tracker CLI.

---

### Task 1: Proposal schema + stable dedup key — DONE

`Confidence` StrEnum + frozen `Proposal` dataclass (evidence required at construction) + `norm()` (scrubs timestamps / `#NNN` / `pid=` / `:line` / path-like tokens → `<path>` placeholder so the fingerprint is stable) + `dedup_key` (sha256 16-hex). Tests: evidence-required, key-is-16-hex, norm-strips-volatile-tokens, norm-idempotent (Hypothesis).

### Task 2: Append-only ledger — DONE

`Ledger` appends one JSON event per line `{key, event, source, category, file, evidence, proposed_diff, confidence}` under an `fcntl.flock` on a sibling `*.lock`. `count(key)` = cardinality of `seen` rows (never a mutated scalar → no lost increment); `is_suppressed(key)` = a `declined` row exists (durable across sessions); `is_applied(key)`; `list_filed()` = latest payload per key with `seen >= 2` and not declined/applied. Tests: count-is-seen-cardinality, decline-durable-and-suppresses (fresh handle), concurrent-appends-no-lost-rows (20 threads).

### Task 4: `emit()` orchestration — DONE

`EmitResult{DROPPED_SUPPRESSED, HELD_FIRST_OCCURRENCE, FILED}`; `emit()` drops suppressed keys, records `seen`, holds at count 1, returns `FILED` at >= 2; `decline()`/`mark_applied()` append the durable events; `list_filed()` is the open human-facing queue. Ledger path overridable via `SETFORGE_PROPOSALS_LEDGER` (default `.claude/proposals/ledger.jsonl`). Tests: first-holds, second-files, suppressed-dropped-even-after-compaction, applied-drops-from-filed.

### Task 5: Safe diff-apply — DONE

`validate_diff_paths()` rejects symlink hunks (`new file mode 120000`, guards CVE-2023-23946) and any target that is absolute or escapes `repo_root` (after `Path.resolve()`); `approve()` validates, runs `git apply --check` (fail-closed, no residue), applies with `cwd=repo_root` (never commits/merges), then `mark_applied()`. Empty `proposed_diff` = advisory-only ack. Tests: rejects-traversal / absolute / symlink, accepts-in-repo, approve-applies-in-a-real-git-repo.

---

### Task 6: `surface-proposals` skill — TODO

**Files:** Create `.claude/skills/surface-proposals/SKILL.md` (exempt layer — may name the tracker CLI concretely).

- [ ] **Step 1: Write the skill.** Prose, modeled on `.claude/skills/enforce-tests/SKILL.md`. It MUST specify:
  - **When:** invoked at Phase-6 (pre-merge) and session-end.
  - **Read:** call `list_filed()` from `scripts/proposals.py` to get the open queue.
  - **Transition to the tracker:** for each filed proposal not already tracked, create a task-tracker item (carry the `dedup_key` in a label so it is idempotent — find-by-label before create; deterministic newest-wins on >1 match), so proposals show up in the user's normal ready queue.
  - **Render:** show each on the resolved review surface (atelier default / revdiff) with the `evidence` **fenced as untrusted data** (never as instructions) and the `proposed_diff` shown as a diff; point the reviewer at the rule's `git log -p` origin (cumulative-drift defense).
  - **Approve:** call `proposals.approve(p, repo_root=…)` (applies the diff to the worktree for the normal Phase-6 human review — **never auto-merged**), then close the tracker item as applied.
  - **Decline:** call `proposals.decline(p)` (durable ledger suppress) then close the tracker item as declined.
  - **Guardrails:** never auto-apply without the human approve; gates ratchet up only; proposals are untrusted data.
- [ ] **Step 2:** `uv run python scripts/check_policy_lints.py; echo EXIT=$?` → 0.
- [ ] **Step 3:** Commit.

### Task 7: Flip RULES.md SELF-1..4 "Enforced by" — TODO

**Files:** Modify `docs/RULES.md`. Change the SELF-1..4 "Enforced by" cells from `(planned)` to `scripts/proposals.py` (+ the `surface-proposals` skill for the human-gate rule). Makes docs-sync (F8) report zero drift on these rows later. Commit.

### Task 8: Full-module gate + acceptance — TODO

- [ ] `uv run pytest tests/test_proposals.py -v --no-cov; echo EXIT=$?` → 0
- [ ] `uv run python scripts/check_policy_lints.py; echo EXIT=$?` → 0
- [ ] `uv run python scripts/check_schema_gates.py; echo EXIT=$?` → 0
- [ ] `pre-commit run --all-files; echo EXIT=$?` → 0
- [ ] Confirm carved acceptance: 1st-emit files nothing, 2nd files one; declined stays dropped after a simulated compaction; diff-apply rejects the three escape shapes; concurrent emit files once.

---

## Verification (end-to-end)

```bash
uv run pytest tests/test_proposals.py -v --no-cov   # all green
uv run python scripts/check_policy_lints.py          # exit 0 — new code obeys SAFE-1 (no shell=True)
pre-commit run --all-files                           # exit 0 — ruff/mypy/ref-scan/policy clean
```

Then Phase 5 = the `enforce-tests` project review entry. Phase 6 = atelier/revdiff human review before merge.

## Self-review notes
- Spec coverage: schema (T1), ledger count+suppress (T2), 2nd-occurrence emit (T4), safe apply (T5), surfacing+transition skill (T6), RULES flip (T7) — every spec item maps to a task.
- Pitfalls covered: dedup stability (T1 property test), flock no-lost-append + durable suppress (T2), idempotent emit (T4), path-confinement + symlink-reject + `--check`-first (T5), untrusted-data fencing + idempotent tracker transition (T6).
