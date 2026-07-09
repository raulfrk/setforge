---
name: reviewing-markdown
description: 1-agent prose review for generic .md files (READMEs, project-level CLAUDE.md, docs/, CHANGELOG.md — anything outside tracked/claude/). Dispatches `markdown-prose-reviewer` via a single Agent tool call. Use after generic-markdown edits (Phase 5), and again post-merge (Phase 7).
---

# Reviewing Markdown

This skill orchestrates a 1-agent prose review for generic markdown files OUTSIDE `tracked/claude/`. Single-agent because the spec / form / substance / specifics aspects do not apply to free-form documentation; only prose quality and factual correctness do. The agent under `~/.claude/agents/` is dispatched via the `Agent` tool **in a single message**.

## When to invoke

- **Phase 5** (review fan) after Phase 4 (Implement) completes and before Phase 6 (Address findings + merge), per the 7-phase flow in the `session-flow` skill.
- **Phase 7** (post-merge cross-cutting review) against the merged HEAD on the target branch.

Artifacts in scope:
- `README.md`, project-level `CLAUDE.md` (NOT under `tracked/claude/`).
- `docs/**/*.md`, `CHANGELOG.md`, ADRs.
- Any other `.md` file outside `tracked/claude/`.

For mixed-artifact PRs (Python + generic .md, or tracked/claude/ .md + generic .md), invoke `reviewing-markdown` alongside `reviewing-python-code` and/or `reviewing-claude-md` via separate Skill invocations. Regardless of artifact mix, `reviewing-bd-leaks` ALSO runs in parallel (task-tracker-leak scan over all file types) — leak-checking is not this fan's job.

## Dispatch inputs

Compute (or accept as input):

| Input | Default |
|---|---|
| `BASE_SHA` | `git merge-base HEAD origin/main` |
| `HEAD_SHA` | `git rev-parse HEAD` |
| `spec_path` | `~/.claude/projects/{cwd-slug}/specs/<YYYY-MM-DD>-<topic>.md` or `(none)` |
| `bd_id` | derive from current branch / worktree slug, or explicit user input |
| `changed_files` | `git diff --name-only $BASE_SHA..$HEAD_SHA` |
| `doc_type` | closed-set per file kind; default `other` |
| `audience` | free-text |
| `purpose` | free-text |
| `examples` | optional URLs / repo paths |
| `research_online` | bool, default `false` |

## Dispatch

Send **a single message** containing 1 `Agent` tool call:

1. `subagent_type: markdown-prose-reviewer` — prose quality + factual correctness for generic .md files outside `tracked/claude/`.

Prompt template:

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
doc_type: <doc_type>
audience: <audience>
purpose: <purpose>
examples: <examples or "(none)">
research_online: <true|false>

Apply your aspect-specific checklist (see your system prompt body).
Return structured findings + DoD checklist + verdict line.
```

## Consolidation

Compress the consolidated report: keep the verdict table (one line per
aspect) and reproduce every CONCERNS or BLOCK finding verbatim; include an
aspect's full sub-report verbatim ONLY when its verdict is not PASS — PASS
aspects compress to their verdict line plus a one-line DoD state.

After the agent returns, produce a single report:

```
# Markdown prose review report (BASE_SHA..HEAD_SHA)

## markdown-prose-reviewer (yellow)
<sub-report verbatim — or verdict line only if PASS>

## Overall
- markdown-prose: <verdict>

Overall verdict: <verdict>
```

## After the report

- CRITICAL findings → fix immediately (especially: false claims about commands / flags / paths).
- IMPORTANT findings → fix before merge. Per the commit conventions in CLAUDE.md, review-fix changes commit as their OWN commits — never squashed into the implementation commit.
- MINOR findings → fix inline as separate commits unless the fix is large or out-of-scope (file new bd with dep link in that case).

## Definition of done for the orchestrator

- [ ] Single dispatch issued via a single message.
- [ ] Agent returned a `Verdict: ...` line.
- [ ] Consolidated report produced — every non-PASS sub-report verbatim; PASS aspects compressed to verdict + DoD state.
- [ ] Verdict stated explicitly.

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
- When you repoint a named external reference in this file — a review-surface name (revdiff/atelier), a cross-referenced skill (session-flow), or a path/file — because of a rename or global migration, grep THIS whole file for the old name and every other named surface/skill/path before finishing, and repoint all of them in the same edit; a single dangling name is the recurring failure.  [c:23dcfa8] [c:7794c3a] [c:0bac4ac]
- When you add or change a conditional rule in a section of this file that also carries a fixed report skeleton, template, or placeholder (e.g. the Consolidation section's <sub-report verbatim> placeholder and DoD line), reconcile every placeholder/label/checklist item in that same section against the new rule in the same edit, so no placeholder still asserts the old unconditional behavior.  [c:fa0603a] [c:4df4aaa]
