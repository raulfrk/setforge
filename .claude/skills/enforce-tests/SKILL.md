---
name: enforce-tests
description: setforge's review-fan orchestrator — invoke at session-flow Phase 5 and Phase 7 on an engine diff, instead of running the global python fan bare. Runs the Tier-1 deterministic gates first (short-circuits the fan on failure), dispatches the two project agents (test-quality-reviewer, design-invariant-reviewer) alongside the global reviewing-python-code + reviewing-bd-leaks fans, and consolidates one two-axis verdict (BLOCKING vs ADVISORY).
---

# enforce-tests

The setforge review-fan orchestrator. It is the **Phase 5 / Phase 7 review entry**
for engine diffs: it runs the deterministic Tier-1 floor, then the advisory LLM
fan, then folds three different block-authorities into **one two-axis verdict**
without flattening the 3-tier funnel (deterministic + human BLOCK; LLM ADVISES).

This skill is **prose you execute** — follow the fixed command shapes and the
explicit control flow exactly; do not improvise gate invocations.

## Inputs — compute ONCE, broadcast to every sub-dispatch

Resolve the inputs up front and pass the once-computed **range** explicitly into
every sub-dispatch. Never let a sub-skill fall back to its own default — the
defaults diverge (the converging fan workflow bases on `merge-base HEAD main`, no
`origin/`). The input *shapes* differ per reviewer (see Step 2): the python fan
and the two project agents take five inputs; `reviewing-bd-leaks` takes its own
four (`BASE_SHA`, `HEAD_SHA`, `changed_files`, `pr_number`).

```sh
BASE_SHA=$(git merge-base HEAD origin/main)   # canonical base — pinned
HEAD_SHA=$(git rev-parse HEAD)
git diff --name-only -z "$BASE_SHA" "$HEAD_SHA" > /tmp/enforce_changed.0
```

- `bd_id` — derive from the worktree slug / branch name, or take it as given.
- `spec_path` — the archived spec under the cwd-slug specs dir, or `(none)`.
- `changed_files` — the NUL-delimited list written above (a real list, broadcast
  to every reviewer; do NOT let a sub-skill recompute its own).

**Assert a clean working tree before running the gates:**

```sh
test -z "$(git status --porcelain)" || { echo "refuse: dirty tree — commit or stash first"; exit 2; }
```

The gates lint the **working tree** (`check_policy_lints.py` reads on-disk content
of tracked files; `check_schema_gates.py` imports the live modules) while the fan
reviews the `BASE..HEAD` **commits**. A dirty tree makes them disagree, so a
BLOCKED could cite a violation the reviewed commits never contained. Refuse rather
than review two different snapshots.

## Step 1 — Tier-1 gates (run FIRST, short-circuit)

Three deterministic gates. Run each **unpiped**, capture `$?` on the very next
line (a pipe through `tail`/`tee`/`grep` reports the pipe's exit, not the gate —
a false green). Convention for all three: **exit 0 = clean (pass), nonzero =
blocked.** Derive pass/fail from `rc`, never from stdout presence.

```sh
uv run python scripts/check_policy_lints.py > /tmp/g_policy.out 2>&1; rc_policy=$?
uv run python scripts/check_schema_gates.py > /tmp/g_schema.out 2>&1; rc_schema=$?
# bd-leak deterministic gate — guard the empty set; rely on the script's own
# [ -f ] skip for deleted paths; xargs chunks under ARG_MAX.
if [ -s /tmp/enforce_changed.0 ]; then
  xargs -0 scripts/check-no-bd-refs.sh < /tmp/enforce_changed.0 > /tmp/g_bdrefs.out 2>&1; rc_bdrefs=$?
else
  echo "(no changed files to scan)"; rc_bdrefs=0   # vacuous, stated — NOT a green claim
fi
```

**INFRA vs violation.** A nonzero is a real violation **only when the gate's own
banner heads its output** — `Policy lint violations` / the schema-gate failure
banner / `bd-leak:`. Any other nonzero (a `uv sync` failure, an import error, a
traceback) is an **INFRA error**: emit `Tier-1: COULD NOT RUN` and stop — never a
content BLOCK. (`uv run` normalizes a crashed script to exit 1, the same code as a
clean violation, so the banner is the discriminator.)

**Whole-repo scope caveat.** `check_policy_lints.py` scans the whole tracked repo
(`git ls-files '*.py'`), not just `changed_files`. A BLOCKED here MAY cite a
pre-existing / unrelated violation — the verdict must say so and tell the reader
to check whether *this diff* introduced it. Do NOT re-scope the gate to
changed-files; the repo-wide block is by design.

**Short-circuit (hard control-flow gate).** If ANY gate is a real violation:
emit the BLOCKED verdict (below) quoting the failing gate's captured output, and
**STOP — do not dispatch the fan.** The fan section below runs only when
`rc_policy == 0 && rc_schema == 0 && rc_bdrefs == 0`.

## Step 2 — Dispatch the review fan (only when Tier-1 is all-zero)

One parallel batch over the same computed range. Reference the global fans **by
skill name** (so a new upstream aspect agent is picked up automatically — never
hard-code the python aspect roster). Each sub-source takes its own input shape —
pass them explicitly, do not rely on a sub-skill's default range:

1. **Skill `reviewing-python-code`** — the 4-aspect python fan. Inputs: `BASE_SHA`,
   `HEAD_SHA`, `changed_files`, `spec_path`, `bd_id`.
2. **Skill `reviewing-bd-leaks`** — the advisory tracker-leak scan. Inputs:
   `BASE_SHA`, `HEAD_SHA`, `changed_files`, `pr_number` (pass `(none)` when not a
   PR) — it does NOT take `spec_path` / `bd_id`. **Owned here** — enforce-tests runs
   it exactly once; the CLAUDE.md manifest tells session-flow not to also dispatch it.
3. **Agent `test-quality-reviewer`** (direct) — the five inputs (as #1).
4. **Agent `design-invariant-reviewer`** (direct) — the five inputs (as #1).

**Partial-failure rule.** Every one of the four sub-sources MUST return a verdict.
A sub-source that dies or returns no `Verdict:` line folds into the consolidation
as **non-PASS (error)** — never silently dropped, so a missing report can never
produce a falsely-clean top verdict.

## Step 3 — Consolidate ONE two-axis verdict

Two axes, so the three block-authorities never collapse into one ambiguous token:

- **BLOCKING axis** — the Tier-1 gate rows + the `reviewing-bd-leaks` result.
  These are the only hard blocks. **Top verdict = BLOCK iff a Tier-1 gate failed
  OR bd-leak blocked.** The BLOCKING axis strictly dominates: an advisory PASS can
  never override a blocking BLOCK.
- **ADVISORY axis** — worst-of-N over `reviewing-python-code`
  (`PASS / CONCERNS / BLOCK`) + `test-quality-reviewer` + `design-invariant-reviewer`
  (`CRITICAL / IMPORTANT / MINOR` findings plus their own verdict line). Define the
  mapping from each source's vocabulary into the top table explicitly. The advisory
  "BLOCK" is **do-not-merge advice to the human (Phase 6) gate, never a CI
  hard-block** — these agents never block directly.

**Sidecars** (carried as their own sections, not flattened into severity):
`test-quality-reviewer`'s per-test `KEEP / CHANGE-DETECTOR / OVER-MOCKED / …`
manifest, and `design-invariant-reviewer`'s `CONCERNS (human gate)` block.

**Pending-floor seam.** The verdict ALWAYS prints, on the BLOCKING axis:

```
coverage / mutation: PENDING F2b  (not yet wired — Tier-1 floor is lints-only)
```

so a lints-only run is never mistaken for the full Tier-1 floor. The F2b work
flips this line when the coverage + mutation gates land.

**Self-improvement pass-through.** Aggregate each agent's one-line
`self_improvement:` note into a final report section. **Pass-through only** — no
backlog, no dedup, no second-occurrence capture, no revdiff surfacing. That loop
is owned by the F7 self-improvement work, not this skill.

## Verdict templates

```
# enforce-tests verdict (BASE_SHA..HEAD_SHA)

## BLOCKING axis
- Tier-1 policy lints : PASS | BLOCKED (may be pre-existing — confirm this diff introduced it) | COULD NOT RUN
- Tier-1 schema gates : PASS | BLOCKED | COULD NOT RUN
- Tier-1 bd-leak gate : PASS | BLOCKED
- bd-leak review fan  : PASS | BLOCKED
- coverage / mutation : PENDING F2b
=> TOP: BLOCK iff any BLOCKED above, else see advisory.

## ADVISORY axis (do-not-merge advice to the human gate; never a CI hard-block)
- reviewing-python-code   : <worst-of-N + findings>
- test-quality-reviewer   : <verdict + findings>   (+ per-test manifest sidecar)
- design-invariant-reviewer: <verdict + findings>  (+ CONCERNS (human gate) sidecar)

## self_improvement (pass-through)
- <note> …
```

On Tier-1 short-circuit, emit only the BLOCKING axis with the failing gate's
output quoted and `FAN SKIPPED (tree cannot merge)`.

## Bugs and code smells this skill must not commit

Gate-invocation:

- **Piped exit code** — run unpiped, capture `$?` on the next line.
- **uv-crash-vs-violation** — use the banner discriminator, not the bare exit code.
- **Dirty-tree snapshot skew** — assert a clean tree before the gates.
- **bd-refs empty / spaced / deleted / huge args** — guard the empty set, NUL + `xargs`, rely on the script's `[ -f ]` skip.
- **policy-lint whole-repo scope** — frame a BLOCKED as possibly pre-existing.
- **Missing short-circuit** — explicit all-zero precondition; BLOCKING dominates.
- **Exit-code inversion** — 0 = pass; derive pass/fail from `$?`, never stdout.

Orchestration:

- **Block-authority conflation** — keep the BLOCKING and ADVISORY axes separate.
- **Base-ref drift** — broadcast the once-computed range to every sub-source.
- **Broadcast fallback gap** — pass each sub-source its full input shape (Step 2), never let it recompute.
- **Severity-vocab mismatch** — map each source's vocabulary explicitly; carry the sidecars.
- **Manifest roster drift** — reference the global fans by skill name, never a hard-coded aspect roster.
- **Cross-repo pointer rot** — the manifest and the session-flow pointer reference each other.
- **Roster double / missed dispatch** — own `reviewing-bd-leaks` once; be the engine review entry.
- **Partial-failure silent drop** — every sub-source returns a verdict or folds to non-PASS.
