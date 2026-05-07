# Superpowers as the leading workflow

Superpowers is the primary working framework on this VM. Its multi-phase
flow — brainstorm → spec → plan → implement → review — is the default
for non-trivial work. The Socratic output style shapes *engagement
within* a phase (question before assert, surface trade-offs, draw out
user reasoning); it does not compete with the phase structure.

This file is imported from `CLAUDE.md` so its directives ride the
`<system-reminder>` "OVERRIDE default behavior" wrapper, which
Superpowers skills are designed to defer to.

## Canonical phase flow

1. **Brainstorm** (`superpowers:brainstorming`) — explore intent,
   requirements, constraints. May produce or sharpen a bd issue.
2. **Spec in plan mode** — write the spec verbatim into the plan body
   (no summary, no reflow). User reviews and annotates. On approval,
   capture load-bearing details into the bd issue's `--design` /
   `--acceptance` / `--notes`.
3. **Plan** — produce the implementation plan as a normal response
   (no `EnterPlanMode`). The bd issue's `--design` / `--acceptance` /
   `--notes` already carries the contract from step 2; the plan is
   internal scaffolding, not a review surface.
4. **Implement** (`superpowers:executing-plans` by default;
   `superpowers:subagent-driven-development` when there are 2+
   genuinely independent threads). Work against the bd issue (the
   contract). TDD where it fits.
5. **Review** (`superpowers:verification-before-completion`, plus
   `superpowers:requesting-code-review` when the change is load-bearing).
   Verify the implementation satisfies the spec, not just that tools
   pass. Out-of-scope findings → new `bd create` with a dep link.

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
  (e.g. `-home-raul-my-setup`).

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
- **Subagent-driven implementation**
  (`superpowers:subagent-driven-development`): off by default.
  Dispatch only when the work has 2+ genuinely independent threads or
  when context isolation is the explicit goal — not as a default for
  serial implementation.

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
