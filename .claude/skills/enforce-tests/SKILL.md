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
the workflow (Step 2). Never let the workflow fall back to its own default — pin
the base to local `merge-base HEAD main` (no `origin/`; local main leads origin
while unpushed). The whole set (`base`, `head`, `changedFiles`, `repo`, `jobTmp`,
`specPath`, `bdId`, `prNumber`, `extraAspects`) rides in one `Workflow` args object;
the workflow fans the aspect agents internally (Step 2), so there is no longer a
per-reviewer dispatch to broadcast to.

```sh
BASE_SHA=$(git merge-base HEAD main)   # canonical base — pinned (local main; origin lags while unpushed)
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

## Step 2 — Run the converging review workflow (only when Tier-1 is all-zero)

Invoke the `reviewing-fan-workflow` (the `review-fan` Workflow) as the advisory
review engine — it fans the file-type-matched aspect reviewers, verifies findings,
applies devil's-advocate-gated fixes, and **unwinds them to one staged diff**. This
REPLACES the old per-agent dispatch; invoking a Workflow from this skill satisfies
the Workflow opt-in (a skill whose instructions call `Workflow`). Pass the
once-computed range explicitly:

```js
Workflow({
  name: 'review-fan',
  args: {
    base: BASE_SHA, head: HEAD_SHA, changedFiles: <the changed_files list>,
    repo: <repo root>, jobTmp: "$CLAUDE_JOB_DIR/tmp",
    specPath: <spec_path>, bdId: <bd_id>, prNumber: <pr_number>,
    extraAspects: ['test-quality-reviewer', 'design-invariant-reviewer'],
  },
})
```

- **`extraAspects` injects the two setforge PROJECT agents** into the workflow's
  fan. The generic `selectAspects` already picks `python-*` + security + concurrency
  + complexity-adversary + `bd-leak-reviewer` by file type — never hard-code that
  roster; the workflow selects it, and `extraAspects` only adds the project pair.
- **bd-leak is still OWNED here, but run ONCE — inside the workflow.** The workflow
  always fans `bd-leak-reviewer`; read its result from the workflow findings rather
  than dispatching `reviewing-bd-leaks` separately. Do NOT also run the leak scan —
  session-flow's manifest still routes the single leak run through this skill.
- The workflow **auto-fixes** confirmed Important+ findings (devil's-advocate-gated)
  and returns them **unwound to one uncommitted STAGED diff** (`fixDiff`) — no
  `fix(review): round N` commits leak into history. It returns
  `{status: clean|stalled|budget|error, confirmedFixed, openResidual, fixDiff}`.

**Partial-failure rule.** If the workflow returns `status: 'error'` (bad args / no
scope) or dies, fold it into the consolidation as **non-PASS (error)** — never a
falsely-clean top verdict.

## Step 3 — Consolidate ONE two-axis verdict

Two axes, so the three block-authorities never collapse into one ambiguous token:

- **BLOCKING axis** — the Tier-1 gate rows + the bd-leak result (**read from the
  workflow's `bd-leak-reviewer` findings**, not a separate dispatch). These are the
  only hard blocks. **Top verdict = BLOCK iff a Tier-1 gate failed OR bd-leak
  blocked.** The BLOCKING axis strictly dominates: an advisory PASS can never
  override a blocking BLOCK.
- **ADVISORY axis** — the workflow's outcome maps to the top table: `clean` ⇒ PASS,
  `stalled` / `budget` ⇒ CONCERNS (findings it couldn't converge), `error` ⇒
  non-PASS. The advisory content is `confirmedFixed` (auto-fixed, staged in
  `fixDiff`) + `openResidual` (couldn't fix). The `test-quality-reviewer` +
  `design-invariant-reviewer` findings arrive INSIDE the fan (via `extraAspects`),
  gated/surfaced like any aspect; their sidecar manifests still carry (below). The
  advisory outcome is **do-not-merge advice to the human (Phase 6) gate, never a CI
  hard-block**.

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

## Step 4 — Serve the Phase-6 gate on Atelier (ALWAYS)

The workflow returns to the orchestrator; **serving the human gate is the
orchestrator's job** (a Workflow runs to completion and cannot block a turn on an
Atelier Submit). This is the mandatory, **un-skippable** Phase-6 review surface —
never fall back to a chat verdict + a "review on Atelier?" offer.

- **Author a verdict page** carrying: the two-axis verdict table (BLOCKING /
  ADVISORY) + the staged **`fixDiff`** (what the fan auto-fixed, for the human to
  approve) + **`openResidual`** (what it couldn't fix) + the sidecar manifests.
- **Serve + wait:**
  `python3 ~/.claude/skills/atelier/scripts/atelier.py publish <page> --id <slug>-review`
  then `atelier.py wait <slug>-review`; END THE TURN on the wait (per the `atelier`
  skill's `/wait` contract).
- **On Submit & Close (approve)** → commit the staged fixes with a real message,
  then proceed to merge. **On plain Submit** → address the notes, re-serve. A
  BLOCKING BLOCK still serves (so the human sees it) but merge is refused until it
  clears.

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
- review-fan workflow      : clean | stalled | budget | error  (=> PASS | CONCERNS | non-PASS)
- confirmedFixed (staged in fixDiff) : <n>   ·   openResidual : <n>
- test-quality-reviewer    : <per-test manifest sidecar>        (ran INSIDE the fan via extraAspects)
- design-invariant-reviewer: <CONCERNS (human gate) sidecar>    (ran INSIDE the fan via extraAspects)

## self_improvement (pass-through)
- <note> …
```

On Tier-1 short-circuit, emit only the BLOCKING axis with the failing gate's
output quoted and `WORKFLOW SKIPPED (tree cannot merge)`. The Phase-6 Atelier gate
(Step 4) still serves the BLOCKING verdict so the human sees the block.

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
- **Base-ref drift** — pass the once-computed range into the workflow args; never let it recompute a default.
- **Args-shape gap** — pass the full args object (Step 2); the workflow fails loud on missing `base`/`head`/`changedFiles`.
- **Outcome-vocab mismatch** — map the workflow `clean`/`stalled`/`budget`/`error` into the top table; carry the sidecars.
- **Aspect-roster drift** — let the workflow's `selectAspects` pick the roster; inject ONLY the project agents via `extraAspects`.
- **Cross-repo pointer rot** — the manifest and the session-flow pointer reference each other.
- **bd-leak double-run** — the workflow fans `bd-leak-reviewer` once; read it from the workflow, never dispatch `reviewing-bd-leaks` separately.
- **Gate-skip regression** — ALWAYS serve the Phase-6 Atelier gate (Step 4); never a chat verdict + a "review on Atelier?" offer.
- **Partial-failure silent drop** — a workflow `error` folds to non-PASS, never a clean top verdict.
