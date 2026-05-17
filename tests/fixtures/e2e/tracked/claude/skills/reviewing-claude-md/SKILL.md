---
name: reviewing-claude-md
description: 5-aspect parallel review fan for CLAUDE.md / workflow-doc changes (tracked/claude/CLAUDE.md, superpowers-prefs.md, header.md, bd-reference / wt-reference / new skill content, agent definitions, additional-content.md). Dispatches `claude-md-spec-reviewer`, `claude-md-form-reviewer`, `claude-md-substance-reviewer`, `claude-md-specifics-reviewer`, and `claude-md-prose-reviewer` in parallel via a single message of Agent tool calls. Consolidates verdicts worst-of-five. Use after CLAUDE.md / workflow-doc implementation (Phase 5), and again post-merge (Phase 7).
---

# Reviewing CLAUDE.md / workflow docs

This skill orchestrates a 5-aspect parallel review fan for CLAUDE.md and related workflow-doc changes. Each aspect is a dedicated specialized agent under `~/.claude/agents/`; this skill dispatches all five in parallel via the `Agent` tool **in a single message**, then consolidates the five reports.

## When to invoke

- **Phase 5** (review fan) after Phase 4 (Implement) completes and before Phase 6 (Address findings + merge), per the 7-phase flow in `superpowers-prefs.md`.
- **Phase 7** (post-merge cross-cutting review) against the merged HEAD on the target branch.

Artifacts in scope (ONLY `.md` files under `tracked/claude/`):
- `tracked/claude/CLAUDE.md`, `tracked/claude/superpowers-prefs.md`, `tracked/claude/header.md`, `tracked/claude/additional-content.md`.
- `tracked/claude/skills/<skill>/SKILL.md` (new or edited skills).
- `tracked/claude/agents/<agent>.md` (new or edited agent definitions).
- `my_setup.yaml` when the change touches deployment of any of the above.

Out of scope: project-level CLAUDE.md (the one at a project's root, NOT under `tracked/claude/`) and any other `.md` file outside `tracked/claude/` belong to `reviewing-markdown`.

For mixed-artifact PRs, invoke every applicable skill in parallel via separate Skill invocations: `reviewing-python-code` when the diff also touches Python source / pyproject / pre-commit / CI workflow, and `reviewing-markdown` when it touches generic `.md` files outside `tracked/claude/` (READMEs, project-level CLAUDE.md, docs/, CHANGELOG.md, ADRs).

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

After all 5 agents return, produce a single report:

```
# CLAUDE.md review fan report (BASE_SHA..HEAD_SHA)

## claude-md-spec-reviewer (red)
<sub-report verbatim>

## claude-md-form-reviewer (blue)
<sub-report verbatim>

## claude-md-substance-reviewer (green)
<sub-report verbatim>

## claude-md-specifics-reviewer (purple)
<sub-report verbatim>

## claude-md-prose-reviewer (yellow)
<sub-report verbatim>

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
- IMPORTANT findings → fix before merge. Per the Commits rule in `~/.claude/CLAUDE.md`, review-fix changes commit as their OWN commits — never squashed into the implementation commit.
- MINOR findings → file new bd issues with dep links unless trivially fixable inline.

## Definition of done for the orchestrator

- [ ] All 5 reviewer dispatches issued in a single message (true parallel).
- [ ] All 5 agents returned a `Verdict: ...` line.
- [ ] Consolidated report produced with all 5 sub-reports verbatim.
- [ ] Worst-of-five overall verdict computed and stated explicitly.
