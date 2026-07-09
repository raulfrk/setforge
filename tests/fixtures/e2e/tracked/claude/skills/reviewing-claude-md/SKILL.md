---
name: reviewing-claude-md
description: 5-aspect parallel review fan for CLAUDE.md / workflow-doc changes (tracked/claude/CLAUDE.md, session-flow, handoff, bd-reference / wt-reference / new skill content, agent definitions). Dispatches `claude-md-spec-reviewer`, `claude-md-form-reviewer`, `claude-md-substance-reviewer`, `claude-md-specifics-reviewer`, and `claude-md-prose-reviewer` in parallel via a single message of Agent tool calls. Consolidates verdicts worst-of-five. Use after CLAUDE.md / workflow-doc implementation (Phase 5), and again post-merge (Phase 7).
---

# Reviewing CLAUDE.md / workflow docs

This skill orchestrates a 5-aspect parallel review fan for CLAUDE.md and related workflow-doc changes. Each aspect is a dedicated specialized agent under `~/.claude/agents/`; this skill dispatches all five in parallel via the `Agent` tool **in a single message**, then consolidates the five reports.

## When to invoke

- **Phase 5** (review fan) after Phase 4 (Implement) completes and before Phase 6 (Address findings + merge), per the 7-phase flow in the `session-flow` skill.
- **Phase 7** (post-merge cross-cutting review) against the merged HEAD on the target branch.

Artifacts in scope (ONLY `.md` files under `tracked/claude/`):
- `tracked/claude/CLAUDE.md`.
- `tracked/claude/skills/<skill>/SKILL.md` (new or edited skills).
- `tracked/claude/agents/<agent>.md` (new or edited agent definitions).
- `setforge.yaml` when the change touches deployment of any of the above.

Out of scope: project-level CLAUDE.md (the one at a project's root, NOT under `tracked/claude/`) and any other `.md` file outside `tracked/claude/` belong to `reviewing-markdown`.

For mixed-artifact PRs, invoke every applicable skill in parallel via separate Skill invocations: `reviewing-python-code` when the diff also touches Python source / pyproject / pre-commit / CI workflow, and `reviewing-markdown` when it touches generic `.md` files outside `tracked/claude/` (READMEs, project-level CLAUDE.md, docs/, CHANGELOG.md, ADRs). Regardless of artifact mix, `reviewing-bd-leaks` ALSO runs in parallel (task-tracker-leak scan over all file types) — leak-checking is not this fan's job.

## Dispatch inputs

Compute (or accept as input):

| Input | Default |
|---|---|
| `BASE_SHA` | `git merge-base HEAD origin/main` |
| `HEAD_SHA` | `git rev-parse HEAD` |
| `spec_path` | `~/.claude/projects/{cwd-slug}/specs/<YYYY-MM-DD>-<topic>.md` or `(none)` |
| `bd_id` | derive from current branch / worktree slug, or explicit user input |
| `changed_files` | `git diff --name-only $BASE_SHA..$HEAD_SHA` |

## Parallel dispatch

Send **a single message** containing 5 `Agent` tool calls, one per reviewer:

1. `subagent_type: claude-md-spec-reviewer` — spec-conformance, file placement, structural commitments.
2. `subagent_type: claude-md-form-reviewer` — header hierarchy, link validity, bullet style, markdown rendering.
3. `subagent_type: claude-md-substance-reviewer` — internal coherence, cross-file consistency, cross-ref accuracy.
4. `subagent_type: claude-md-specifics-reviewer` — meta-twist, user-section markers, enforcement-layer correctness, observation coverage.
5. `subagent_type: claude-md-prose-reviewer` — prose quality + factual correctness vs. referenced skill / tool surfaces.

Use the same prompt template across all five:

```
You are reviewing the diff in BASE_SHA..HEAD_SHA.
BASE_SHA: <sha>
HEAD_SHA: <sha>
spec_path: <path or "(none)">
bd_id: <bd-id>
changed_files:
- <file>
- <file>
...

Apply your aspect-specific checklist (see your system prompt body).
Return structured findings + DoD checklist + verdict line.
```

## Consolidation

Compress the consolidated report: keep the verdict table (one line per
aspect) and reproduce every CONCERNS or BLOCK finding verbatim; include an
aspect's full sub-report verbatim ONLY when its verdict is not PASS — PASS
aspects compress to their verdict line plus a one-line DoD state.

After all 5 agents return, produce a single report:

```
# CLAUDE.md review fan report (BASE_SHA..HEAD_SHA)

## claude-md-spec-reviewer (red)
<sub-report verbatim — or verdict line only if PASS>

## claude-md-form-reviewer (blue)
<sub-report verbatim — or verdict line only if PASS>

## claude-md-substance-reviewer (green)
<sub-report verbatim — or verdict line only if PASS>

## claude-md-specifics-reviewer (purple)
<sub-report verbatim — or verdict line only if PASS>

## claude-md-prose-reviewer (yellow)
<sub-report verbatim — or verdict line only if PASS>

## Overall
- claude-md-spec: <verdict>
- claude-md-form: <verdict>
- claude-md-substance: <verdict>
- claude-md-specifics: <verdict>
- claude-md-prose: <verdict>

Overall verdict: <worst-of-five — BLOCK > CONCERNS > PASS>
```

## After the report

- CRITICAL findings → fix immediately (especially: edits to live files, broken user-section markers, missing structural commitments).
- IMPORTANT findings → fix before merge. Per the commit conventions in CLAUDE.md, review-fix changes commit as their OWN commits — never squashed into the implementation commit.
- MINOR findings → fix inline as separate commits unless the fix is large or out-of-scope (file new bd with dep link in that case).

## Definition of done for the orchestrator

- [ ] All 5 reviewer dispatches issued in a single message (true parallel).
- [ ] All 5 agents returned a `Verdict: ...` line.
- [ ] Consolidated report produced — every non-PASS sub-report verbatim; PASS aspects compressed to verdict + DoD state.
- [ ] Worst-of-five overall verdict computed and stated explicitly.

## Self-improvement

While using this skill, stay alert for any *generic* way it could be better — clearer wording, a missing case, a smoother step, a recurring friction it should prevent. Not only failures; any worthwhile improvement, noticed anytime.

- **At a completion checkpoint** (a finished unit of work before the next, or session end), pause and, if anything surfaced, propose it as a diff to THIS file via atelier — one edit per idea, citing what prompted it.
<!-- si-core:start -->
- **Don't edit mid-task.** Capture the observation; keep working.
- **Generic only.** Global config used across every project; never bake in project-specific detail (paths, repo/profile names, bead IDs) unless the artifact is itself project-scoped.
- **Never auto-apply.** Propose via atelier; the user approves every edit. Never write it yourself.
- **Off-limits — never propose edits to:** hard rails, the `## Environment` / safety sections, system paths, `setforge:user-section` marker lines or their `hash=`, and *this self-improvement protocol itself* (the mechanism may not rewrite its own leash).
- **Substantive, not noise.** Rare and load-bearing; not cosmetic rewording; never re-propose a declined idea.
<!-- si-core:end -->

Levers (this artifact):
- If the scope list in this file (Artifacts in scope / Out of scope / mixed-artifact) ever names a `.md` target that CLAUDE.md's Phase-5 dispatch guidance routes to a DIFFERENT review skill, treat the mismatch as a finding: propose realigning this file's scope boundary so the two never claim or disown the same file — never let both this skill and reviewing-markdown claim project-level CLAUDE.md.  [c:ce1ea67] [c:06784b7]
- If you are about to recommend adding ONE new cross-cutting reviewer to this fan (a single agent spanning a new aspect) because DRY suggests it, stop and instead recommend the paired `claude-md-<aspect>-reviewer` matrix entry that mirrors how the existing five are structured — surface the single-agent option only as an explicitly-flagged trade-off, never as the default.  [mem:reviewer_matrix_pattern]
- When computing the `BASE_SHA` default from the Dispatch-inputs table for a branch whose local main is many commits ahead of origin/main, do NOT ship `git merge-base HEAD origin/main` unchanged — override to the merge-base against LOCAL main (or HEAD..main) so the review diff covers only this branch's changes and not a large stale range.  [mem:enforce_tests_base_ref_local_main]
