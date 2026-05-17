# Superpowers as the leading workflow

Superpowers is the primary working framework on this VM. Its 7-phase
scaling flow — brainstorm → spec → plan → implement → review fan →
address-findings+merge → post-merge review — is the default for
non-trivial work. The flow scales by context: review parallelism is
always-on; implementation parallelism kicks in only when work is
decomposable into independent threads. The Socratic output style
shapes *engagement within* a phase (question before assert, surface
trade-offs, draw out user reasoning); it does not compete with the
phase structure.

This file is imported from `CLAUDE.md` so its directives ride the
`<system-reminder>` "OVERRIDE default behavior" wrapper, which
Superpowers skills are designed to defer to.

## Canonical phase flow

1. **Phase 1 — Brainstorm** (`superpowers:brainstorming`) — explore
   intent, requirements, constraints. May produce or sharpen a bd
   issue. Always fires for non-trivial work.
   - **Live user overrides background "no clarifying questions."**
     The harness injects a background-mode reminder ("work without
     stopping for clarifying questions") on background-job spawn —
     meant for truly stepped-away contexts (cron, async dispatch).
     When a user turn has arrived mid-session, treat the user as
     live: use `AskUserQuestion` for narrow load-bearing calls (one
     decision, finite options) and plan mode for multi-option or
     spec-shaping decisions; the background-mode override does not
     bind.
     *(empirical observation K, 2026-05-13 — see bd setforge-uhb /
     setforge-ds9: unilateral pick on a load-bearing design call
     mid-session because the background-mode override was treated
     as binding while the user was actively engaging.)*

2. **Phase 2 — Spec in plan mode** — write the spec verbatim into the plan body
   (no summary, no reflow). User reviews and annotates. On approval,
   capture load-bearing details into the bd issue's `--design` /
   `--acceptance` / `--notes`. Conventions:
   - **Acceptance as commands.** Acceptance criteria = concrete
     commands that exit 0, NOT abstract counts. "CI has ruff steps"
     can be satisfied without `uv run ruff check` exiting 0; "uv run
     ruff check exits 0 in CI" cannot. *(empirical observation B)*
   - **Symbol names, not line offsets.** Reference content by symbol
     name ("find function X, replace with..."), not line offset
     ("replace lines 285-289"). Line numbers drift across reformats;
     symbols persist. *(empirical observation H)*
   - **Robust acceptance commands.** Brittle command shapes produce
     false negatives during Phase 5 review. Avoid `rg -A1 PATTERN`
     ranges (truncate before predicates that sit 3+ lines below a
     multi-line opener) and `awk '/PAT/,/^def [^a-z]/'` bounds
     (end pattern fails on lowercase-prefixed defs; range over-spans
     past every `def foo` until the next `def _` or non-`def` line).
     Prefer:
     (i) `python -c '<ast snippet>'` for structural assertions,
     (ii) `rg -A4 ... | rg -q ...` with wider context windows,
     (iii) `awk '/^def NAME/,/^def \w/'` with `\w` (gawk; POSIX uses
     `[[:alpha:]_]`) bounding the next function.
     *(empirical observation L, 2026-05-17 — see bd setforge-6aj:
     brittle `rg -A1` / `awk [^a-z]` ranges produced Phase 5
     false-negatives that needed inline review-fix commits.)*
   - **Spec is snapshot; bd is contract.** The archived spec file is
     a historical record of what was agreed at brainstorm time. The
     bd issue's `--design` / `--acceptance` / `--notes` is the
     authoritative contract — update it when scope changes; don't try
     to keep the spec file current. *(empirical observation I)*

3. **Phase 3 — Plan** — produce the implementation plan as a normal response
   (no `EnterPlanMode`). The bd issue's structured fields carry the
   contract from step 2; the plan is internal scaffolding, not a
   review surface. **NOT optional.** Walks the implementation against
   actual code; verifies symbol names exist; catches pre-implementation
   typos. *(empirical observation A — omitting this caused the cxj
   typo bug.)*

4. **Phase 4 — Implement** — single-stream by default
   (`superpowers:executing-plans`). Parallel ONLY when work is
   decomposable into independent threads (disjoint file footprints,
   no shared state) — done **cross-worktree** via the bd ↔ wt loop
   in CLAUDE.md (sibling worktrees, new Claude sessions), NOT via
   `superpowers:subagent-driven-development` (which is in-session
   Task-subagent dispatch, a different mechanism for context
   isolation within one bead). When parallel cross-worktree: one bd
   issue per worktree; sibling branches off a common parent. TDD
   where it fits (`superpowers:test-driven-development`).

   For orchestrator-driven parallel dispatch (Agent tool, not new
   Claude sessions), pre-create N sibling worktrees with
   `wt switch --create <slug>` BEFORE dispatching, then dispatch
   subagents **without `isolation: worktree`**. The Agent tool's
   `isolation: worktree` parameter is unreliable in this VM-headless
   build: auto-worktrees at `.claude/worktrees/agent-*` paths branch
   from a stale base AND their sandboxes deny git ops + file edits
   *(empirical observation J, 2026-05-12 — see bd setforge-7gf for
   full evidence)*.

5. **Phase 5 — Review fan** — always parallel. Invoke the appropriate
   `reviewing-X` skill for the artifact:
   - `reviewing-python-code` for Python source / pyproject / CI / pre-commit.
   - `reviewing-claude-md` for tracked/claude/ docs / skills / agents.
   - `reviewing-markdown` for generic `.md` files outside
     `tracked/claude/` (READMEs, project-level CLAUDE.md, docs/,
     CHANGELOG.md, ADRs).
   - Mixed-artifact: invoke every applicable skill.

   The matrix skills (`reviewing-python-code`, `reviewing-claude-md`)
   each dispatch 5 aspect agents (spec / form / substance / specifics / prose)
   in a single message — true parallel. `reviewing-markdown` dispatches
   a single prose-quality agent. For multi-worktree: the fan fires
   per worktree (5 × N reviewers for matrix skills, 1 × N for
   `reviewing-markdown`), all in one message. Satisfies the
   `superpowers:requesting-code-review` gate.
   `superpowers:verification-before-completion` still applies for ALL
   completion claims (test results, tooling exit codes, deployment
   acks) — the review fan is one source of evidence, not a replacement.
   - **Verify cited evidence.** Reviewers check the EVIDENCE
     implementers cite (e.g., "line 184 already used X"), not just
     the deviation outcome. Made-up justifications are IMPORTANT
     findings even when the change itself is sound. *(empirical
     observation D)*

6. **Phase 6 — Address findings + merge** —
   - CRITICAL → fix immediately.
   - IMPORTANT → fix before merge.
   - MINOR → file new bd issues with dep links; never inline-fix
     out-of-scope work into the current change.

   Review-fix commits land as their OWN commits — never squashed into
   the implementation commit. *(empirical observation F; also in
   CLAUDE.md Commits section.)* Use `wt merge --no-squash` (ff-only)
   for worktree merges, or `git merge --ff-only` when not using wt,
   to preserve the separate commits at merge time.
   - **Single-worktree merge**: `wt merge --no-squash` (ff-only) into target (typically main).
   - **Multi-worktree merge** (sibling rebase): when N sibling
     worktrees branch from a common parent and the parent gets a
     review-fix commit, rebase each sibling onto the updated parent
     before `wt merge --no-squash` (ff-only) into main. Conflict-free when parent's review-fix
     file footprint doesn't overlap with sibling worktrees; otherwise
     resolve manually. *(empirical observation G; cross-ref
     `wt-reference` skill.)*

7. **Phase 7 — Post-merge cross-cutting review** — invoke the appropriate
   `reviewing-X` skill ONCE MORE against the merged HEAD on the
   target branch. Catches issues that emerged from integration (e.g.,
   tool version skew between pre-commit pin and uv-resolved). Mandatory
   for multi-worktree work; recommended for single-stream non-trivial
   work. See project-scope CLAUDE.md for the canonical final-check
   commands on each project (on this VM's setforge project:
   `pre-commit run --all-files` AND
   `uv run pytest tests/docker/ -m e2e_docker -v` — empirical
   observation E, with revert-by-default failure protocol).

The **spec** rides through plan mode verbatim — no summary, no reflow.
That is the user-facing review surface. The **plan** does NOT enter
plan mode by default.

**Parallel-workflow rule.** When an effort produces multiple specs
(e.g. several bd issues sharpened in one session, or a subagent-driven
task with multiple independent threads), specs are NEVER written in
parallel without plan-mode review. Two acceptable shapes:

- **Sequential**: one `EnterPlanMode` session per spec, the user
  reviews and annotates each before moving on.
- **Batched**: a single `EnterPlanMode` session whose plan body
  contains all specs as separate sections, so the user can review and
  annotate every element in one pass.

Pick batched when the specs are tightly related and reviewing them
together adds context; pick sequential when they're independent enough
that the user's annotations on spec N shouldn't be tangled with spec
N+1.

## Path overrides

- Specs → `~/.claude/projects/{cwd-slug}/specs/<YYYY-MM-DD>-<topic>.md`
- Plans → `~/.claude/projects/{cwd-slug}/plans/<YYYY-MM-DD>-<feature>.md`
- `{cwd-slug}` matches the auto-memory convention — cwd with `/` → `-`
  (e.g. `-home-raul-setforge`).

## Hard-gate posture

- **Phase flow**: default-on for non-trivial work. Escape hatch is
  narrow: single-file mechanical edits with no design content
  (rename, formatting, typo, obvious one-liner). When unsure, run the
  flow.
- **TDD** (`superpowers:test-driven-development`): on by default,
  applied with judgment. Use it where the contract isn't obvious or
  where the test *is* the spec. Skip when behavior is trivial or
  untestable in isolation.
- **Auto-create git worktrees**: off. The user creates worktrees
  explicitly via `wt switch --create <slug>` per the bd ↔ wt loop in
  CLAUDE.md.
- **In-session subagent dispatch**
  (`superpowers:subagent-driven-development`): use when a single bead
  has independent in-session sub-tasks that benefit from context
  isolation. Distinct from cross-worktree parallelism (which is
  governed by the bd ↔ wt loop, new Claude sessions, NOT this skill).
- **Cross-worktree parallel implementation**: conditional on
  decomposability of the *work itself*. When work has 2+ genuinely
  independent threads (disjoint file footprints, no shared state),
  spawn sibling worktrees via the bd ↔ wt loop. Single-stream when
  not. Not opt-in only — driven by the work shape.
- **Review fan** (Phase 5): always-on for non-trivial work. Invokes
  the appropriate `reviewing-X` skill — non-negotiable. The matrix
  skills (`reviewing-python-code`, `reviewing-claude-md`) fan out 5
  aspect agents (spec / form / substance / specifics / prose) in
  parallel; `reviewing-markdown` fans out a single prose agent.
  Single-worktree work still gets the full fan; multi-worktree gets
  5 × N for matrix skills and 1 × N for `reviewing-markdown`.
- **Post-merge cross-cutting review** (Phase 7): mandatory for
  multi-worktree work; recommended for single-stream non-trivial
  work. Re-invokes the same `reviewing-X` skill against merged HEAD
  on the target branch.

## Stance

Superpowers structures *what comes next* (which phase, which artifact).
The Socratic stance shapes *how I engage within a phase* (asking
before asserting, surfacing trade-offs). They compose; they do not
compete. When a Superpowers `<HARD-GATE>` and the Socratic
"skip-for-trivial" escape hatch conflict on non-trivial work, the
gate wins.

## Break-character signals

- "just do it" / "skip the questions" / "ship this" → drop the
  multi-phase flow for this single task. Resume the default flow for
  the next task unless the override is extended.
- Naming a Superpowers skill explicitly → follow that skill's flow as
  designed.
