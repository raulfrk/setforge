---
name: reviewing-markdown
description: 1-agent prose review for generic .md files (READMEs, project-level CLAUDE.md, docs/, CHANGELOG.md — anything outside tracked/claude/). Dispatches `markdown-prose-reviewer` via a single Agent tool call. Use after generic-markdown edits (Phase 5), and again post-merge (Phase 7).
---

# Reviewing Markdown

This skill orchestrates a 1-agent prose review for generic markdown files OUTSIDE `tracked/claude/`. Single-agent because the spec / form / substance / specifics aspects do not apply to free-form documentation; only prose quality and factual correctness do. The agent under `~/.claude/agents/` is dispatched via the `Agent` tool **in a single message**.

## When to invoke

- **Phase 5** (review fan) after Phase 4 (Implement) completes and before Phase 6 (Address findings + merge), per the 7-phase flow in `superpowers-prefs.md`.
- **Phase 7** (post-merge cross-cutting review) against the merged HEAD on the target branch.

Artifacts in scope:
- `README.md`, project-level `CLAUDE.md` (NOT under `tracked/claude/`).
- `docs/**/*.md`, `CHANGELOG.md`, ADRs.
- Any other `.md` file outside `tracked/claude/`.

For mixed-artifact PRs (Python + generic .md, or tracked/claude/ .md + generic .md), invoke `reviewing-markdown` alongside `reviewing-python-code` and/or `reviewing-claude-md` via separate Skill invocations.

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

After the agent returns, produce a single report:

```
# Markdown prose review report (BASE_SHA..HEAD_SHA)

## markdown-prose-reviewer (yellow)
<sub-report verbatim>

## Overall
- markdown-prose: <verdict>

Overall verdict: <verdict>
```

## After the report

- CRITICAL findings → fix immediately (especially: false claims about commands / flags / paths).
- IMPORTANT findings → fix before merge. Per the Commits rule in `~/.claude/CLAUDE.md`, review-fix changes commit as their OWN commits — never squashed into the implementation commit.
- MINOR findings → file new bd issues with dep links unless trivially fixable inline.

## Definition of done for the orchestrator

- [ ] Single dispatch issued via a single message.
- [ ] Agent returned a `Verdict: ...` line.
- [ ] Consolidated report produced with the sub-report verbatim.
- [ ] Verdict stated explicitly.
